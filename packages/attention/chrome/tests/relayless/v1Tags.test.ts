// @vitest-environment node
import { IDBFactory, IDBObjectStore, IDBDatabase } from "fake-indexeddb";
import { describe, expect, test, vi } from "vitest";
import { V1Journal, type V1Gate } from "../../src/relayless/v1Journal";
import { V1Http, V1HttpError } from "../../src/relayless/v1Http";
import { resolveV1Tag, tagCreateName } from "../../src/relayless/v1Tags";
const ACCOUNT="11111111-2222-4333-8444-555555555555", OTHER="22222222-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const TAG="33333333-4444-4555-8666-777777777777", DEST=`Event/${TAG}`, HASH="a".repeat(64);
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:false};
async function setup(){const indexedDB=new IDBFactory();const journal=new V1Journal({indexedDB});await journal.transition(null,gate);return {journal,indexedDB};}
const absent=()=>new V1HttpError("http_error",404);
function transport(json:ReturnType<typeof vi.fn>,scope=gate){return {scope,json,assertJournal:()=>{}};}

describe("durable account-scoped v1 tag journal",()=>{
 test("upgrades v1 without losing its gate, generations, or pending datatype intent",async()=>{
  const indexedDB=new IDBFactory();
  await new Promise<void>((resolve,reject)=>{const r=indexedDB.open("fulcra-attention-v1",1);
   r.onupgradeneeded=()=>{const db=r.result;const s=db.createObjectStore("gate");s.put({version:1,initialized:true},"format");s.put(gate,"current");db.createObjectStore("generations").put(true,GEN);db.createObjectStore("creations").put({version:1,accountId:ACCOUNT,specKey:"collect.attention.v1",fingerprint:HASH,status:"pending",createdUnderGeneration:GEN,destination:null},[ACCOUNT,"collect.attention.v1"]);};
   r.onerror=()=>reject(r.error);r.onsuccess=()=>{r.result.close();resolve();};});
  const a=new V1Journal({indexedDB});expect(await a.readGate()).toEqual(gate);
  expect((await a.readCreation(gate,"collect.attention.v1",HASH))?.status).toBe("pending");
  expect((await a.reserveTag(gate,"attention","attention")).created).toBe(true);
  const changed={...gate,generation:NEXT};await a.transition(gate,changed);
  await expect(a.transition(changed,gate)).rejects.toThrow();await a.close();
 });
 test("only one context reserves a create; restart never resets an unknown attempt",async()=>{
  const {journal:a,indexedDB}=await setup();const b=new V1Journal({indexedDB});
  const rows=await Promise.all([a.reserveTag(gate,"attention","attention"),b.reserveTag(gate,"attention","attention")]);
  expect(rows.map(x=>x.created).sort()).toEqual([false,true]);await a.close();
  expect((await b.reserveTag(gate,"attention","attention")).created).toBe(false);await b.close();
 });
 test("same-account generation reconciles immutable UUID, other account cannot adopt it",async()=>{
  const {journal}=await setup();await journal.reserveTag(gate,"attention","attention");
  const next={...gate,generation:NEXT};await journal.transition(gate,next);
  await expect(journal.resolveTag(gate,"attention","attention",TAG)).rejects.toThrow();
  await journal.resolveTag(next,"attention","attention",TAG);
  await expect(journal.resolveTag(next,"attention","attention",OTHER)).rejects.toThrow();
  await expect(journal.readTag(next,"attention","changed")).rejects.toThrow();
  const other={...next,accountId:OTHER,generation:"cccccccc-dddd-4eee-8fff-000000000000"};await journal.transition(next,other);
  expect(await journal.readTag(other,"attention","attention")).toBeNull();await journal.close();
 });
 test.each(["", " ","bad\u0000label", "\ud800", "x".repeat(1025)])("invalid label refuses %j",async label=>{
  const {journal}=await setup();await expect(journal.reserveTag(gate,label,"valid")).rejects.toThrow();await journal.close();
 });
 test.each(["", "bad/name", "x".repeat(31),"\udfff"])("invalid create-name refuses %j",async name=>{
  const {journal}=await setup();await expect(journal.reserveTag(gate,"label",name)).rejects.toThrow();await journal.close();
 });
 test("quota fault rolls back reservation and a retry is still first",async()=>{
  const {journal}=await setup();const original=IDBObjectStore.prototype.add;
  const fault=vi.spyOn(IDBObjectStore.prototype,"add").mockImplementation(function(this:IDBObjectStore,value,key){if(this.name==="tags")throw new DOMException("synthetic quota","QuotaExceededError");return original.call(this,value,key);});
  try{await expect(journal.reserveTag(gate,"label","label")).rejects.toThrow();}finally{fault.mockRestore();}
  expect(await journal.readTag(gate,"label","label")).toBeNull();expect((await journal.reserveTag(gate,"label","label")).created).toBe(true);await journal.close();
 });
 test("binding cannot be invented without a prior reservation or accept Event destinations",async()=>{
  const {journal}=await setup();await expect(journal.resolveTag(gate,"label","label",TAG)).rejects.toThrow();
  await journal.reserveTag(gate,"label","label");await expect(journal.resolveTag(gate,"label","label",DEST)).rejects.toThrow();await journal.close();
 });
 test("ignored strict durability cannot reserve, submit, or resolve a tag",async()=>{
  const {journal}=await setup();await journal.reserveTag(gate,"first","first");const original=IDBDatabase.prototype.transaction;
  const fault=vi.spyOn(IDBDatabase.prototype,"transaction").mockImplementation(function(this:IDBDatabase,names,mode){return original.call(this,names,mode);});
  try{await expect(journal.reserveTag(gate,"second","second")).rejects.toThrow();await expect(journal.submitTag(gate,"first","first",TAG)).rejects.toThrow();await expect(journal.resolveTag(gate,"first","first",TAG)).rejects.toThrow();}finally{fault.mockRestore();}
  expect((await journal.readTag(gate,"first","first"))?.status).toBe("pending");expect(await journal.readTag(gate,"second","second")).toBeNull();await journal.close();
 });
 test("full tag journal holds its oldest unknown attempt and refuses eviction",async()=>{
  const {journal,indexedDB}=await setup();await journal.reserveTag(gate,"first","first");await journal.close();
  await new Promise<void>((resolve,reject)=>{const r=indexedDB.open("fulcra-attention-v1");r.onerror=()=>reject(r.error);r.onsuccess=()=>{
   const tx=r.result.transaction("tags","readwrite");for(let n=1;n<1000;n++)tx.objectStore("tags").add({version:1,accountId:ACCOUNT,label:`label-${n}`,name:`name-${n}`,status:"pending",createdUnderGeneration:GEN,destination:null},[ACCOUNT,`label-${n}`]);
   tx.oncomplete=()=>{r.result.close();resolve();};tx.onabort=()=>reject(tx.error);};});
  const reopened=new V1Journal({indexedDB});await expect(reopened.reserveTag(gate,"overflow","overflow")).rejects.toThrow();expect((await reopened.readTag(gate,"first","first"))?.status).toBe("pending");await reopened.close();
 });
 test("corrupt submitted state is unknown, never a fresh reservation",async()=>{
  const {journal,indexedDB}=await setup();await journal.reserveTag(gate,"first","first");await journal.close();
  await new Promise<void>((resolve,reject)=>{const r=indexedDB.open("fulcra-attention-v1");r.onerror=()=>reject(r.error);r.onsuccess=()=>{
   const tx=r.result.transaction("tags","readwrite");tx.objectStore("tags").put({version:1,accountId:ACCOUNT,label:"first",name:"first",status:"submitted",createdUnderGeneration:GEN,destination:null},[ACCOUNT,"first"]);
   tx.oncomplete=()=>{r.result.close();resolve();};};});
  const reopened=new V1Journal({indexedDB});await expect(reopened.readTag(gate,"first","first")).rejects.toThrow();await expect(reopened.reserveTag(gate,"first","first")).rejects.toThrow();await reopened.close();
 });
 test("malformed older database cannot be upgraded into an invented empty authority",async()=>{
  const indexedDB=new IDBFactory();await new Promise<void>((resolve,reject)=>{const r=indexedDB.open("fulcra-attention-v1",1);r.onupgradeneeded=()=>r.result.createObjectStore("gate");r.onerror=()=>reject(r.error);r.onsuccess=()=>{r.result.close();resolve();};});
  const journal=new V1Journal({indexedDB});await expect(journal.readGate()).rejects.toThrow();
  await new Promise<void>((resolve,reject)=>{const r=indexedDB.open("fulcra-attention-v1",1);r.onerror=()=>reject(r.error);r.onsuccess=()=>{expect(r.result.objectStoreNames.contains("tags")).toBe(false);r.result.close();resolve();};});
 });
 test("version2 missing the unique name index cannot publish a tag reservation",async()=>{
  const indexedDB=new IDBFactory();await new Promise<void>((resolve,reject)=>{const r=indexedDB.open("fulcra-attention-v1",2);r.onupgradeneeded=()=>{for(const name of ["gate","generations","creations","tags"])r.result.createObjectStore(name);};r.onerror=()=>reject(r.error);r.onsuccess=()=>{r.result.close();resolve();};});
  await expect(new V1Journal({indexedDB}).reserveTag(gate,"label","label")).rejects.toThrow();
 });
});
describe("v1 tag names and one-shot cloud resolution",()=>{
 test("transport and tag binding cannot use separate authority journals",async()=>{
  const {journal}=await setup();const other=(await setup()).journal;let calls=0;
  const token="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":ACCOUNT,exp:1700003600})).toString("base64url")+".signature";
  const http=new V1Http({scope:gate,journal,readToken:async()=>token,now:()=>1700000000000,fetch:async url=>{calls++;return Response.json(String(url).endsWith("/info")?{userid:ACCOUNT}:{id:TAG});}});
  await expect(resolveV1Tag({http,journal:other},"attention")).rejects.toThrow();expect(calls).toBe(0);await other.close();await journal.close();
 });
 test.each([".",".."]) ("dot-segment label %j is explicitly unsupported before HTTP or journal effects",async label=>{
  const {journal}=await setup();const json=vi.fn();await expect(tagCreateName(label)).rejects.toThrow(/label is invalid/);
  await expect(resolveV1Tag({http:transport(json),journal},label)).rejects.toThrow(/label is invalid/);expect(json).not.toHaveBeenCalled();await journal.close();
 });
 test("competing resolver contexts can dispatch at most one creation",async()=>{
  const {journal,indexedDB}=await setup();const other=new V1Journal({indexedDB});let reads=0,posts=0;
  const json=vi.fn(async(_path:string,options?:{method?:string})=>{if(options?.method==="POST"){posts++;return {id:TAG};}if(++reads<=2)throw absent();return {id:TAG};});
  const results=await Promise.allSettled([resolveV1Tag({http:transport(json),journal},"attention"),resolveV1Tag({http:transport(json),journal:other},"attention")]);
  expect(posts).toBe(1);expect(results.some(row=>row.status==="fulfilled")).toBe(true);expect((await journal.readTag(gate,"attention","attention"))?.destination).toBe(TAG);await journal.close();await other.close();
 });
 test.each([{},[],{id:"bad"},{id:TAG,userid:OTHER},{id:TAG,name:"different"}])("unreadable positive lookup cannot create or bind %j",async result=>{
  const {journal}=await setup();const json=vi.fn().mockResolvedValue(result);await expect(resolveV1Tag({http:transport(json),journal},"attention")).rejects.toThrow();expect(json).toHaveBeenCalledTimes(1);expect(await journal.readTag(gate,"attention","attention")).toBeNull();await journal.close();
 });
 test("a user-info404 is not evidence that the target tag is absent",async()=>{
  const {journal}=await setup();let proofs=0;const calls:string[]=[];
  const token="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":ACCOUNT,exp:1700003600})).toString("base64url")+".signature";
  const http=new V1Http({scope:gate,journal,readToken:async()=>token,now:()=>1700000000000,
   fetch:async(url)=>{calls.push(String(url));if(String(url).endsWith("/info")){if(++proofs===1)return new Response("",{status:404});return Response.json({userid:ACCOUNT});}return Response.json({id:TAG});}});
  await expect(resolveV1Tag({http,journal},"attention")).rejects.toThrow();
  expect(calls).toHaveLength(1);expect(await journal.readTag(gate,"attention","attention")).toBeNull();await journal.close();
 });
 test("short Unicode names count codepoints and aliases match Python",async()=>{
  expect(await tagCreateName("😀".repeat(30))).toBe("😀".repeat(30));
  expect(await tagCreateName("attention")).toBe("attention");
  expect(await tagCreateName("a/b")).toBe("a-b-c14cddc033f64b9dea80");
  expect((await tagCreateName("X".repeat(31))).length).toBeLessThanOrEqual(30);
 });
 test("positive lookup creates a durable account binding without POST",async()=>{
  const {journal}=await setup();const json=vi.fn().mockResolvedValue({id:TAG,name:"attention",userid:ACCOUNT});
  expect(await resolveV1Tag({http:transport(json),journal},"attention")).toBe(TAG);
  expect(json).toHaveBeenCalledTimes(1);expect((await journal.readTag(gate,"attention","attention"))?.destination).toBe(TAG);await journal.close();
 });
 test("only404 creates after durable reservation and readback is required",async()=>{
  const {journal}=await setup();let calls=0;
  const json=vi.fn(async(_path:string,options?:{method?:string})=>{calls++;
   if(calls===1)throw absent();
   expect((await journal.readTag(gate,"attention","attention"))?.status).toBe(options?.method==="POST"?"pending":"submitted");
   if(options?.method==="POST")return {id:TAG};return {id:TAG,name:"attention"};});
  expect(await resolveV1Tag({http:transport(json),journal},"attention")).toBe(TAG);expect(calls).toBe(3);await journal.close();
 });
 test.each([401,403,409,422,429,500])("HTTP%s lookup cannot create",async status=>{
  const {journal}=await setup();const json=vi.fn().mockRejectedValue(new V1HttpError("http_error",status));
  await expect(resolveV1Tag({http:transport(json),journal},"attention")).rejects.toThrow();expect(json).toHaveBeenCalledTimes(1);expect(await journal.readTag(gate,"attention","attention")).toBeNull();await journal.close();
 });
 test("lost POST response stays pending across restart and cannot POST again",async()=>{
  const {journal,indexedDB}=await setup();const json=vi.fn().mockRejectedValueOnce(absent()).mockRejectedValueOnce(new Error("lost"));
  await expect(resolveV1Tag({http:transport(json),journal},"attention")).rejects.toThrow();await journal.close();
  const reopened=new V1Journal({indexedDB});const retry=vi.fn().mockRejectedValue(absent());
  await expect(resolveV1Tag({http:transport(retry),journal:reopened},"attention")).rejects.toThrow();expect(retry).toHaveBeenCalledTimes(1);
  const found=vi.fn().mockResolvedValue({id:TAG});expect(await resolveV1Tag({http:transport(found),journal:reopened},"attention")).toBe(TAG);await reopened.close();
 });
 test("known immutable binding survives cloud rename without creating replacement",async()=>{
  const {journal}=await setup();await journal.reserveTag(gate,"attention","attention");await journal.resolveTag(gate,"attention","attention",TAG);
  const json=vi.fn();expect(await resolveV1Tag({http:transport(json),journal},"attention")).toBe(TAG);expect(json).not.toHaveBeenCalled();await journal.close();
 });
 test.each([{id:OTHER},{id:TAG,name:"different"},{id:TAG,userid:OTHER},{id:TAG,deleted_at:"2026-01-01"},{}])("bad post/readback pair retains submitted identity %j",async result=>{
  const {journal}=await setup();const json=vi.fn().mockRejectedValueOnce(absent()).mockResolvedValueOnce({id:TAG}).mockResolvedValueOnce(result);
  await expect(resolveV1Tag({http:transport(json),journal},"attention")).rejects.toThrow();expect((await journal.readTag(gate,"attention","attention"))?.status).toBe("submitted");await journal.close();
 });
 test("a known POST UUID cannot be substituted after failed readback and restart",async()=>{
  const {journal,indexedDB}=await setup();const json=vi.fn().mockRejectedValueOnce(absent()).mockResolvedValueOnce({id:TAG}).mockRejectedValueOnce(absent());
  await expect(resolveV1Tag({http:transport(json),journal},"attention")).rejects.toThrow();await journal.close();
  const reopened=new V1Journal({indexedDB});const changed=vi.fn().mockResolvedValue({id:OTHER});
  await expect(resolveV1Tag({http:transport(changed),journal:reopened},"attention")).rejects.toThrow();
  expect((await reopened.readTag(gate,"attention","attention"))?.destination).toBe(TAG);await reopened.close();
 });
 test("colliding create-names cannot reserve two label-specific POSTs",async()=>{
  const {journal,indexedDB}=await setup();const other=new V1Journal({indexedDB});
  const outcomes=await Promise.allSettled([journal.reserveTag(gate,"first","same"),other.reserveTag(gate,"second","same")]);
  expect(outcomes.filter(row=>row.status==="fulfilled")).toHaveLength(1);await journal.close();await other.close();
 });
 test("a changed account while reading cannot bind a late lookup result",async()=>{
  const {journal}=await setup();const json=vi.fn(async()=>{await journal.transition(gate,{...gate,accountId:OTHER,generation:NEXT});return {id:TAG};});
  await expect(resolveV1Tag({http:transport(json),journal},"attention")).rejects.toThrow();await journal.close();
 });
});
