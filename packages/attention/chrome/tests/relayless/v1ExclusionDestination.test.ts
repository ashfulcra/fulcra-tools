// @vitest-environment node
import {IDBFactory,IDBObjectStore} from "fake-indexeddb";
import {describe,expect,test,vi} from "vitest";
import {V1Http} from "../../src/relayless/v1Http";
import {V1Journal,type V1Gate} from "../../src/relayless/v1Journal";
import {exclusionV1Contract,exclusionV1Fingerprint} from "../../src/relayless/v1ExclusionSpec";
import {resolveExclusionV1,verifyExclusionV1} from "../../src/relayless/v1ExclusionDestination";
const ACCOUNT="11111111-2222-4333-8444-555555555555",OTHER="22222222-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const TARGET="Event/33333333-4444-4555-8666-777777777777",ALTERNATE="Event/44444444-5555-4666-8777-888888888888";
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:false};
const contract=exclusionV1Contract(),KEY=contract.key;
function schema(){const value=exclusionV1Contract();return {type:"object",properties:{id:{type:"string",format:"uuid"},
 sources:{type:"array",items:{type:"string"}},tags:{type:"array",items:{type:"string",format:"uuid"}},
 start_time:{type:"string",format:"date-time"},end_time:{type:"string",format:"date-time"},...value.fields},required:value.required_fields};}
function entry(id=TARGET,name=contract.name):any{return {id,name,api_version:"v1",recordable:true,deprecated:false,
 record_spec:{type:"event",schema:JSON.stringify(schema())}};}
async function setup(initial=true){
 const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB});await journal.transition(null,gate);
 const state={catalog:initial?[entry()]:[],schema:schema() as any,posts:0,reads:0,schemaReads:0,
  mode:"redirect",publish:true,beforePost:null as null|(()=>Promise<void>),duringSchema:null as null|(()=>Promise<void>),
  duringOwned:null as null|(()=>Promise<void>)};
 const paths:string[]=[];
 const token="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":ACCOUNT,exp:1700003600})).toString("base64url")+".signature";
 const makeHttp=(scope=gate,authority=journal)=>new V1Http({scope,journal:authority,readToken:async()=>token,now:()=>1700000000000,
  fetch:async(url,options={})=>{const path=new URL(String(url)).pathname;paths.push(path);
   if(path.endsWith("/info"))return Response.json({userid:ACCOUNT});
   if(path==="/input/v1/data_type"){state.reads++;await state.duringOwned?.();return Response.json(state.catalog);}
   if(path.endsWith("/schema")){state.schemaReads++;await state.duringSchema?.();return Response.json(state.schema);}
   if(options.method==="POST"&&path==="/input/v1/data_type/Event"){
    state.posts++;await state.beforePost?.();
    if(state.publish)state.catalog.push(entry());
    if(state.mode==="lost")throw new Error("synthetic lost response");
    if(state.mode==="receipt")return Response.json({id:TARGET});
    if(state.mode==="wrongReceipt")return Response.json({id:ALTERNATE});
    return new Response(null,{status:303});
   }throw new Error("unexpected synthetic route");}});
 return {journal,indexedDB,state,paths,makeHttp,http:makeHttp()};
}
describe("durable browser replay-exclusion control binding",()=>{
 test("read-only verification cannot discover or resolve a missing original intent",async()=>{
  const s=await setup();await expect(verifyExclusionV1({http:s.http,journal:s.journal})).rejects.toThrow();
  expect(s.paths).toEqual([]);expect(await s.journal.readCreation(gate,KEY,await exclusionV1Fingerprint())).toBeNull();await s.journal.close();
 });
 test("read-only verification requires resolved authority and performs no local writes",async()=>{
  const s=await setup();const binding=await resolveExclusionV1({http:s.http,journal:s.journal});
  const put=vi.spyOn(IDBObjectStore.prototype,"put"),add=vi.spyOn(IDBObjectStore.prototype,"add");try{
   const proof=await verifyExclusionV1({http:s.http,journal:s.journal,pinnedId:TARGET});
   expect(proof.dataType).toBe(binding.dataType);expect(proof.reservationGeneration).toBe(GEN);
   expect(put).not.toHaveBeenCalled();expect(add).not.toHaveBeenCalled();
  }finally{put.mockRestore();add.mockRestore();await s.journal.close();}
 });
 test("fresh closed ownership/schema proof and final input recheck precede local binding",async()=>{
  const {journal,http,state,paths}=await setup();const result=await resolveExclusionV1({http,journal});
  expect(result.dataType).toBe(TARGET);expect(result.accountId).toBe(ACCOUNT);expect(result.generation).toBe(GEN);
  expect(result.schemaText).toBe(JSON.stringify(state.schema));expect(result.inputSchemaText).toBe(state.catalog[0].record_spec.schema);
  expect(result.specFingerprint).toBe(await exclusionV1Fingerprint());expect(Object.isFrozen(result)).toBe(true);
  expect(state.reads).toBe(2);expect(state.schemaReads).toBe(1);expect(state.posts).toBe(0);
  expect(paths).not.toContain("/data/v1/catalog");expect((await journal.readCreation(gate,KEY,await exclusionV1Fingerprint()))?.destination).toBe(TARGET);
  await journal.close();
 });
 test("a renamed control is found by schema and is never replaced with an empty control",async()=>{
  const {journal,http,state}=await setup();state.catalog[0].name="Renamed policy";
  expect((await resolveExclusionV1({http,journal,allowCreate:true})).dataType).toBe(TARGET);expect(state.posts).toBe(0);await journal.close();
 });
 test("missing discovery requires explicit creation permission",async()=>{
  const {journal,http,state}=await setup(false);await expect(resolveExclusionV1({http,journal})).rejects.toThrow();
  expect(state.posts).toBe(0);expect(await journal.readCreation(gate,KEY,await exclusionV1Fingerprint())).toBeNull();await journal.close();
 });
 test.each(["redirect","lost","receipt"])("one-shot %s creation is reserved before dispatch and reconciled by fresh reads",async mode=>{
  const {journal,http,state}=await setup(false);state.mode=mode;
  state.beforePost=async()=>{expect((await journal.readCreation(gate,KEY,await exclusionV1Fingerprint()))?.status).toBe("pending");};
  expect((await resolveExclusionV1({http,journal,allowCreate:true})).dataType).toBe(TARGET);expect(state.posts).toBe(1);await journal.close();
 });
 test("unknown creation survives restart; empty discovery never retries POST",async()=>{
  const {journal,indexedDB,http,state,makeHttp}=await setup(false);state.mode="lost";state.publish=false;
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();await journal.close();
  const reopened=new V1Journal({indexedDB}),current=makeHttp(gate,reopened);
  await expect(resolveExclusionV1({http:current,journal:reopened,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(1);
  state.catalog=[entry(TARGET,"Renamed after dispatch")];expect((await resolveExclusionV1({http:current,journal:reopened})).dataType).toBe(TARGET);
  expect(state.posts).toBe(1);await reopened.close();
 });
 test("known receipt is retained before visibility and cannot be substituted",async()=>{
  const {journal,http,state}=await setup(false);state.mode="receipt";state.publish=false;
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();
  expect((await journal.readCreation(gate,KEY,await exclusionV1Fingerprint()))?.destination).toBe(TARGET);
  state.catalog=[entry(ALTERNATE)];await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(1);await journal.close();
 });
 test("account switch immediately after verified POST response cannot lose its known UUID",async()=>{
  const {journal,http,state,makeHttp}=await setup(false);state.mode="receipt";state.publish=false;
  const original=http.json.bind(http),changed={...gate,accountId:OTHER,generation:NEXT};
  vi.spyOn(http,"json").mockImplementation(async(path,options)=>{const result=await original(path,options);
   if(options?.method==="POST")await journal.transition(gate,changed);return result;});
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();
  const restored={...gate,generation:"cccccccc-dddd-4eee-8fff-000000000000"};await journal.transition(changed,restored);
  expect((await journal.readCreation(restored,KEY,await exclusionV1Fingerprint()))?.destination).toBe(TARGET);
  state.catalog=[entry(ALTERNATE)];await expect(resolveExclusionV1({http:makeHttp(restored),journal,allowCreate:true})).rejects.toThrow();
  expect(state.posts).toBe(1);await journal.close();
 });
 test("positive mismatched receipt cannot adopt a different discovered UUID",async()=>{
  const {journal,http,state}=await setup(false);state.mode="wrongReceipt";
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();
  expect((await journal.readCreation(gate,KEY,await exclusionV1Fingerprint()))?.destination).toBe(ALTERNATE);expect(state.posts).toBe(1);await journal.close();
 });
 test("multiple compatible controls require an explicit owned pin",async()=>{
  const {journal,http,state}=await setup();state.catalog.push(entry(ALTERNATE,"Other policy"));
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();
  expect((await resolveExclusionV1({http,journal,pinnedId:ALTERNATE})).dataType).toBe(ALTERNATE);expect(state.posts).toBe(0);await journal.close();
 });
 test("resolved original pin survives rename, while retargeting and retired pin hold",async()=>{
  const {journal,http,state}=await setup();await resolveExclusionV1({http,journal});state.catalog=[entry(TARGET,"Renamed"),entry(ALTERNATE)];
  expect((await resolveExclusionV1({http,journal})).dataType).toBe(TARGET);
  await expect(resolveExclusionV1({http,journal,pinnedId:ALTERNATE,allowCreate:true})).rejects.toThrow();state.catalog[0].deprecated=true;
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(0);await journal.close();
 });
 test.each(["missingSchema","malformedSchema","arraySchema","missingProperties","duplicateConstraint","retired","incompatible","wrongBase","missingName"])("unknown/incompatible discovery holds without creation: %s",async kind=>{
  const {journal,http,state}=await setup();const row=state.catalog[0];
  if(kind==="missingSchema")delete row.record_spec.schema;
  if(kind==="malformedSchema")row.record_spec.schema="{";
  if(kind==="arraySchema")row.record_spec.schema="[]";
  if(kind==="missingProperties")row.record_spec.schema='{"type":"object"}';
  if(kind==="duplicateConstraint")row.record_spec.schema=row.record_spec.schema.replace('"const":1','"const":2,"const":1');
  if(kind==="retired"){row.deprecated=true;row.name="Renamed retired policy";}
  if(kind==="incompatible"){const value=schema();value.properties.reason.enum=["unsupported"] as any;row.record_spec.schema=JSON.stringify(value);row.name="Renamed incompatible policy";}
  if(kind==="wrongBase"){row.id=TARGET.replace("Event/","Metric/");row.record_spec.type="metric";}
  if(kind==="missingName")delete row.name;
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(0);
  expect(await journal.readCreation(gate,KEY,await exclusionV1Fingerprint())).toBeNull();await journal.close();
 });
 test("unrelated Event still needs readable input schema before absence authorizes creation",async()=>{
  const {journal,http,state}=await setup(false);state.catalog=[entry(ALTERNATE,"Other Event")];state.catalog[0].record_spec.schema="bad";
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(0);await journal.close();
 });
 test("bounded discovery refuses more than64 Event candidates, while explicit pin can recover",async()=>{
  const {journal,http,state}=await setup();for(let i=1;i<=64;i++)state.catalog.push(entry(`Event/00000000-0000-4000-8000-${String(i).padStart(12,"0")}`,"Other"));
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();
  expect((await resolveExclusionV1({http,journal,pinnedId:TARGET})).dataType).toBe(TARGET);expect(state.posts).toBe(0);await journal.close();
 });
 test.each(["inputChanged","retired","servedChanged","generation"])("late proof change cannot publish binding: %s",async kind=>{
  const {journal,http,state}=await setup();state.duringSchema=async()=>{
   if(kind==="inputChanged")state.catalog[0].record_spec.schema+=" ";
   if(kind==="retired")state.catalog[0].deprecated=true;
   if(kind==="servedChanged")state.schema.properties.reason.enum=["unsupported"];
   if(kind==="generation")await journal.transition(gate,{...gate,generation:NEXT});
  };
  await expect(resolveExclusionV1({http,journal})).rejects.toThrow();
  if(kind!=="generation")expect(await journal.readCreation(gate,KEY,await exclusionV1Fingerprint())).toBeNull();await journal.close();
 });
 test.each(["duplicate","unreadable"])("an unbound final discovery must still be unique and readable: %s",async kind=>{
  const {journal,http,state}=await setup();state.duringSchema=async()=>{
   const added=entry(ALTERNATE,"Other policy");if(kind==="unreadable")added.record_spec.schema="{";state.catalog.push(added);
  };
  await expect(resolveExclusionV1({http,journal})).rejects.toThrow();expect(await journal.readCreation(gate,KEY,await exclusionV1Fingerprint())).toBeNull();await journal.close();
 });
 test("a submitted receipt cannot bypass the unresolved ambiguity on retry",async()=>{
  const {journal,http,state}=await setup(false);state.mode="receipt";
  state.duringSchema=async()=>{state.catalog.push(entry(ALTERNATE,"Other control"));state.duringSchema=null;};
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();
  expect((await journal.readCreation(gate,KEY,await exclusionV1Fingerprint()))?.status).toBe("submitted");
  await expect(resolveExclusionV1({http,journal})).rejects.toThrow();
  expect((await journal.readCreation(gate,KEY,await exclusionV1Fingerprint()))?.status).toBe("submitted");
  expect((await resolveExclusionV1({http,journal,pinnedId:TARGET})).dataType).toBe(TARGET);expect(state.posts).toBe(1);await journal.close();
 });
 test("caller options are captured before hashing; another journal cannot publish",async()=>{
  const {journal,http,state}=await setup();state.catalog.push(entry(ALTERNATE));const options={http,journal,pinnedId:TARGET,allowCreate:false};
  const pending=resolveExclusionV1(options);options.pinnedId=ALTERNATE;options.allowCreate=true;
  expect((await pending).dataType).toBe(TARGET);
  const other=new V1Journal({indexedDB:new IDBFactory()});await other.transition(null,gate);
  await expect(resolveExclusionV1({http,journal:other})).rejects.toThrow();await other.close();await journal.close();
 });
 test("same-account fresh generation may reconcile the original unresolved intent",async()=>{
  const {journal,http,state,makeHttp}=await setup(false);state.mode="lost";state.publish=false;
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();const current={...gate,generation:NEXT};await journal.transition(gate,current);
  state.catalog=[entry()];expect((await resolveExclusionV1({http:makeHttp(current),journal})).dataType).toBe(TARGET);expect(state.posts).toBe(1);await journal.close();
 });
 test("an account switch during creation cannot bind; fresh original-account authority reconciles without another POST",async()=>{
  const {journal,http,state,makeHttp}=await setup(false);const changed={...gate,accountId:OTHER,generation:NEXT};
  state.beforePost=async()=>{await journal.transition(gate,changed);};
  await expect(resolveExclusionV1({http,journal,allowCreate:true})).rejects.toThrow();expect(state.posts).toBe(1);
  const restored={...gate,generation:"cccccccc-dddd-4eee-8fff-000000000000"};await journal.transition(changed,restored);
  expect((await resolveExclusionV1({http:makeHttp(restored),journal})).dataType).toBe(TARGET);expect(state.posts).toBe(1);await journal.close();
 });
 test("two contexts cannot dispatch duplicate control creation",async()=>{
  const {journal,indexedDB,http,state,makeHttp}=await setup(false),other=new V1Journal({indexedDB});
  const results=await Promise.allSettled([resolveExclusionV1({http,journal,allowCreate:true}),resolveExclusionV1({http:makeHttp(gate,other),journal:other,allowCreate:true})]);
  expect(results.some(row=>row.status==="fulfilled")).toBe(true);expect(state.posts).toBe(1);await journal.close();await other.close();
 });
 test.each(["owned","schema"])("one monotonic budget refuses late %s proof before local binding",async phase=>{
  const {journal,http,state}=await setup();let now=100;const clock=vi.spyOn(performance,"now").mockImplementation(()=>now);
  try{
   const delay=async()=>{now=111;};if(phase==="owned")state.duringOwned=delay;else state.duringSchema=delay;
   await expect(resolveExclusionV1({http,journal,timeoutMs:10})).rejects.toThrow();expect(state.posts).toBe(0);
   expect(await journal.readCreation(gate,KEY,await exclusionV1Fingerprint())).toBeNull();
  }finally{clock.mockRestore();await journal.close();}
 });
 test("blocked IndexedDB work returns at setup deadline and cannot publish when unblocked",async()=>{
  const {journal,indexedDB,http,state}=await setup();
  const db=await new Promise<IDBDatabase>((resolve,reject)=>{const request=indexedDB.open("fulcra-attention-v1");
   request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(new Error("synthetic open failure"));});
  let hold=true;const tx=db.transaction("creations","readwrite"),store=tx.objectStore("creations");
  const repeat=()=>{const request=store.get("synthetic keepalive");request.onsuccess=()=>{if(hold)repeat();};};repeat();
  const pending=resolveExclusionV1({http,journal,timeoutMs:10});const settled=pending.then(()=>"accepted",()=>"held");
  try{expect(await Promise.race([settled,new Promise(resolve=>setTimeout(()=>resolve("still waiting"),60))])).toBe("held");}
  finally{hold=false;await settled;db.close();}
  expect(state.posts).toBe(0);expect(await journal.readCreation(gate,KEY,await exclusionV1Fingerprint())).toBeNull();await journal.close();
 });
});
