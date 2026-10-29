// @vitest-environment node
import {IDBFactory} from "fake-indexeddb";
import {expect,test} from "vitest";
import {V1Http} from "../../src/relayless/v1Http";
import {V1Journal} from "../../src/relayless/v1Journal";
import {exclusionV1Contract,exclusionV1Fingerprint} from "../../src/relayless/v1ExclusionSpec";
import {buildV1ExclusionRecord} from "../../src/relayless/v1Exclusions";
import {readV1Exclusions} from "../../src/relayless/v1ExclusionPolicy";
import {readProtectionFixture} from "./v1ReadProtectionFixture";
import fixtures from "../../../tests/fixtures/v1_exclusion_cases.json";
const ACCOUNT="11111111-2222-4333-8444-555555555555",OTHER="22222222-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const fixture=fixtures.cases[2],CONTROL=fixture.input.controlType,TARGET=fixture.input.targetDataType,ID=fixture.input.targetRecordId;
const gate={version:1 as const,accountId:ACCOUNT,generation:GEN,enabled:true};
function schema(){const c=exclusionV1Contract();return {type:"object",properties:{id:{type:"string",format:"uuid"},
 sources:{type:"array",items:{type:"string"}},tags:{type:"array",items:{type:"string",format:"uuid"}},
 start_time:{type:"string",format:"date-time"},end_time:{type:"string",format:"date-time"},...c.fields},required:c.required_fields};}
async function setup(bound=true){
 const indexedDB=new IDBFactory(),journal=new V1Journal({indexedDB});await journal.transition(null,gate);
 const hash=await exclusionV1Fingerprint();if(bound){await journal.reserveCreation(gate,exclusionV1Contract().key,hash);await journal.resolveCreation(gate,exclusionV1Contract().key,hash,CONTROL);}
 const protection=readProtectionFixture();
 const state={rows:[structuredClone(fixture.record)] as any[],schema:schema() as any,
  input:JSON.stringify(schema()),deprecated:false,ownedVisible:true,status:200,headers:{} as Record<string,string>,
  body:null as string|null,posts:0,recordReads:0,schemaReads:0,afterRecords:null as null|(()=>Promise<void>),
  duringSchema:null as null|(()=>Promise<void>)};
 const paths:string[]=[];
 const token="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":ACCOUNT,exp:1700003600})).toString("base64url")+".signature";
 const http=new V1Http({scope:gate,journal,readToken:async()=>token,now:()=>1700000000000,readProtection:protection.protection,
  fetch:async(url,options={})=>{const u=new URL(String(url));paths.push(u.pathname);
   if(options.method!=="GET"){state.posts++;throw new Error("synthetic policy read cannot POST");}
   if(u.pathname.endsWith("/info"))return Response.json({userid:ACCOUNT});
   if(u.pathname==="/input/v1/data_type")return Response.json(state.ownedVisible?[{id:CONTROL,name:"Renamed policy",api_version:"v1",recordable:true,
    deprecated:state.deprecated,record_spec:{type:"event",schema:state.input}}]:[]);
   if(u.pathname.endsWith("/schema")){state.schemaReads++;await state.duringSchema?.();return Response.json(state.schema);}
   if(u.pathname==="/data/v1/records"){state.recordReads++;expect(u.searchParams.get("q")).toContain(CONTROL);await state.afterRecords?.();
    return new Response(state.body??state.rows.map(row=>JSON.stringify(row)).join("\n"),{status:state.status,headers:{"content-type":"application/x-jsonl",...state.headers}});}
   throw new Error("unexpected synthetic path");}});
 return {journal,indexedDB,http,state,paths,protection};
}
test("fresh original-control proof brackets protected exact policy and returns frozen scope-bound one-read evidence",async()=>{
 const s=await setup();const result=await readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]});
 expect(result.accountId).toBe(ACCOUNT);expect(result.generation).toBe(GEN);expect(result.controlDataType).toBe(CONTROL);expect(result.targetDataType).toBe(TARGET);
 expect(result.exclusions[ID]).toEqual(fixture.record);expect(Object.isFrozen(result)).toBe(true);expect(Object.isFrozen(result.exclusions[ID].evidence)).toBe(true);
 expect(s.state.schemaReads).toBe(2);expect(s.state.recordReads).toBe(1);expect(s.state.posts).toBe(0);await s.journal.close();
});
test("an unbound control cannot be implicitly created or adopted during a policy read",async()=>{
 const s=await setup(false);await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();
 expect(s.paths).toEqual([]);expect(s.state.posts).toBe(0);await s.journal.close();
});
test("a submitted control receipt is not a resolved policy binding",async()=>{
 const s=await setup(false),c=exclusionV1Contract(),hash=await exclusionV1Fingerprint();await s.journal.reserveCreation(gate,c.key,hash);await s.journal.retainCreationReceipt(gate,c.key,hash,CONTROL);
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();expect(s.paths).toEqual([]);await s.journal.close();
});
test("empty complete protected stream is only an empty policy mapping at this read",async()=>{
 const s=await setup();s.state.rows=[];const result=await readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID,ID]});
 expect(result.exclusions).toEqual({});expect(Object.isFrozen(result.exclusions)).toBe(true);expect(s.state.posts).toBe(0);await s.journal.close();
});
test("equivalent microsecond offsets and root server additions retain a valid permanent exclusion",async()=>{
 const s=await setup();s.state.rows[0].start_time=s.state.rows[0].end_time="2026-01-02T01:15:00.123456+01:15";
 s.state.rows[0].server_extra={synthetic:true};const result=await readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]});
 expect(result.exclusions[ID].server_extra).toEqual({synthetic:true});expect(Object.isFrozen(result.exclusions[ID].server_extra)).toBe(true);await s.journal.close();
});
test.each(["fulcra_userid","userid"])("contradictory row owner %s cannot be treated as harmless server metadata",async name=>{
 const s=await setup();s.state.rows[0][name]=OTHER;
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();expect(s.state.posts).toBe(0);await s.journal.close();
});
test("matching supplied row owner metadata retains a valid policy",async()=>{
 const s=await setup();s.state.rows[0].fulcra_userid=s.state.rows[0].userid=ACCOUNT;
 expect((await readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).exclusions[ID].id).toBe(fixture.record.id);await s.journal.close();
});
test.each(["missing","submitted","pending","generation"].flatMap(mode=>["records","finalSchema"].map(phase=>[mode,phase])))
 ("a policy read cannot repair original authority that becomes %s during %s",async(mode,phase)=>{
 const s=await setup();const corrupt=async()=>{await new Promise<void>((resolve,reject)=>{
  const request=s.indexedDB.open("fulcra-attention-v1");request.onerror=()=>reject(new Error("synthetic open"));request.onsuccess=()=>{
   const db=request.result,tx=db.transaction("creations","readwrite"),store=tx.objectStore("creations"),key=[ACCOUNT,exclusionV1Contract().key];
   if(mode==="missing")store.delete(key);else{const read=store.get(key);read.onsuccess=()=>store.put({...read.result,
    ...(mode==="generation"?{createdUnderGeneration:NEXT}:mode==="pending"?{status:"pending",destination:null}:{status:"submitted"})},key);}
   tx.oncomplete=()=>{db.close();resolve();};tx.onabort=()=>{db.close();reject(new Error("synthetic mutation"));};
  };});};
 if(phase==="records")s.state.afterRecords=corrupt;else s.state.duringSchema=async()=>{if(s.state.schemaReads===2)await corrupt();};
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();
 const intent=await s.journal.readCreation(gate,exclusionV1Contract().key,await exclusionV1Fingerprint());
 if(mode==="generation")expect(intent?.createdUnderGeneration).toBe(NEXT);else expect(intent?.status??null).toBe(mode==="missing"?null:mode);await s.journal.close();
});
test.each([0,-1,NaN,Infinity,30001])("invalid whole policy timeout %s refuses before provider",async timeoutMs=>{
 const s=await setup();await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID],timeoutMs})).rejects.toThrow();expect(s.paths).toEqual([]);await s.journal.close();
});
test("caller choices are detached before the first digest",async()=>{
 const s=await setup(),options={http:s.http,journal:s.journal,targetDataType:TARGET,recordIds:[ID]};
 const pending=readV1Exclusions(options);options.targetDataType=TARGET.replace("Event/","Metric/");options.recordIds.length=0;
 expect((await pending).exclusions[ID]).toEqual(fixture.record);await s.journal.close();
});
test.each(["wrongJournal","badTarget","badId","empty","tooMany","self"])("unsupported policy scope refuses before provider: %s",async kind=>{
 const s=await setup(),other=new V1Journal({indexedDB:new IDBFactory()});await other.transition(null,gate);
 const options={http:s.http,journal:kind==="wrongJournal"?other:s.journal,targetDataType:kind==="badTarget"?"Event":kind==="self"?CONTROL:TARGET,
  recordIds:kind==="badId"?["bad"]:kind==="empty"?[]:kind==="tooMany"?Array(33).fill(ID):[ID]};
 await expect(readV1Exclusions(options)).rejects.toThrow();expect(s.paths).toEqual([]);await other.close();await s.journal.close();
});
test.each(["schemaVersion","targetType","targetId","action","reason","evidence","source","tags","endTime","id"])("malformed stored policy never authorizes absence: %s",async kind=>{
 const s=await setup(),row=s.state.rows[0];
 if(kind==="schemaVersion")row.schema_version=true;if(kind==="targetType")row.target_data_type=TARGET.replace("Event/","Metric/");
 if(kind==="targetId")row.target_record_id=OTHER;if(kind==="action")row.action_id="bad";if(kind==="reason")row.reason="restore";
 if(kind==="evidence")row.evidence=[false];if(kind==="source")row.sources=["different policy target"];
 if(kind==="tags")row.tags=[OTHER];if(kind==="endTime")row.end_time="2026-01-02T00:00:01Z";if(kind==="id")row.id=OTHER;
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();expect(s.state.posts).toBe(0);await s.journal.close();
});
test.each(["partial","encoded","malformedSuffix","duplicate","protection"])("degraded record proof holds policy: %s",async kind=>{
 const s=await setup();if(kind==="partial")s.state.status=206;if(kind==="encoded")s.state.headers["content-encoding"]="gzip";
 if(kind==="malformedSuffix")s.state.body=JSON.stringify(fixture.record)+"\nnot json";
 if(kind==="duplicate")s.state.rows.push(structuredClone(fixture.record));if(kind==="protection")s.protection.state.rules.pop();
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();expect(s.state.posts).toBe(0);await s.journal.close();
});
test.each(["generation","account","disabled","retired","inputChanged","servedChanged","receiptConflict"])("a late scope/control change holds policy proof: %s",async kind=>{
 const s=await setup();s.state.afterRecords=async()=>{
  if(kind==="generation"||kind==="account")await s.journal.transition(gate,{...gate,generation:NEXT,accountId:kind==="account"?OTHER:ACCOUNT});
  if(kind==="disabled")await s.journal.transition(gate,{...gate,generation:NEXT,enabled:false});
  if(kind==="receiptConflict"){try{await s.journal.retainCreationReceipt(gate,exclusionV1Contract().key,await exclusionV1Fingerprint(),`Event/${OTHER}`);}catch{}}
  if(kind==="retired")s.state.deprecated=true;if(kind==="inputChanged")s.state.input+=" ";if(kind==="servedChanged")s.state.schema.description="Changed annotation";
 };
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();await s.journal.close();
});
test("provider fragments never escape a failed policy read",async()=>{
 const s=await setup();s.state.body="SYNTHETIC_PRIVATE_POLICY_FRAGMENT";
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.not.toThrow(/PRIVATE|FRAGMENT/);await s.journal.close();
});
test("a contradictory retained receipt prevents use of the old control",async()=>{
 const s=await setup();await expect(s.journal.retainCreationReceipt(gate,exclusionV1Contract().key,await exclusionV1Fingerprint(),`Event/${OTHER}`)).rejects.toThrow();
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();expect(s.paths).toEqual([]);await s.journal.close();
});
test("Metric targets and32 exact exclusions preserve target mapping",async()=>{
 const s=await setup(),target=TARGET.replace("Event/","Metric/"),ids=Array.from({length:32},(_,i)=>`00000000-0000-4000-8000-${String(i).padStart(12,"0")}`);
 s.state.rows=await Promise.all(ids.map(id=>buildV1ExclusionRecord({...fixture.input,targetDataType:target,targetRecordId:id})));
 const result=await readV1Exclusions({...s,targetDataType:target,recordIds:ids});expect(Object.keys(result.exclusions)).toEqual(ids);expect(s.state.recordReads).toBe(1);await s.journal.close();
});
test.each(["missing","retired","incompatible"])("existing control that is %s cannot be replaced or skipped",async kind=>{
 const s=await setup();if(kind==="missing")s.state.ownedVisible=false;if(kind==="retired")s.state.deprecated=true;
 if(kind==="incompatible")s.state.schema.properties.reason.enum=["restore"];
 await expect(readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID]})).rejects.toThrow();expect(s.state.recordReads).toBe(0);expect(s.state.posts).toBe(0);await s.journal.close();
});
test("blocked local authority returns at the whole policy deadline without later network effects",async()=>{
 const s=await setup();const db=await new Promise<IDBDatabase>((resolve,reject)=>{const r=s.indexedDB.open("fulcra-attention-v1");r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(new Error("synthetic open"));});
 let hold=true;const tx=db.transaction("creations","readwrite"),store=tx.objectStore("creations");
 const repeat=()=>{const r=store.get("synthetic keepalive");r.onsuccess=()=>{if(hold)repeat();};};repeat();
 const result=readV1Exclusions({...s,targetDataType:TARGET,recordIds:[ID],timeoutMs:10}).then(()=>"accepted",()=>"held");
 try{expect(await Promise.race([result,new Promise(resolve=>setTimeout(()=>resolve("still waiting"),60))])).toBe("held");}
 finally{hold=false;await result;db.close();}
 await s.journal.readGate();expect(s.paths).toEqual([]);await s.journal.close();
});
