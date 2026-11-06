// @vitest-environment node
import { describe, expect, test, vi } from "vitest";
import { V1Http } from "../../src/relayless/v1Http";
import type { V1Gate } from "../../src/relayless/v1Journal";
import { readV1OwnedTypes } from "../../src/relayless/v1OwnedTypes";
const ACCOUNT="11111111-2222-4333-8444-555555555555", OTHER="22222222-3333-4444-8555-666666666666";
const GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GEN,enabled:false};
const destination=`Event/${OTHER}`;
const entry=(change:Record<string,unknown>={})=>({id:destination,name:"Attention",api_version:"v1",recordable:true,deprecated:false,record_spec:{type:"event"},...change});
function setup(result:()=>Response=()=>Response.json([entry()])) {
 const calls:{url:string,init:RequestInit}[]=[],state={gate:{...gate}};
 const token="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":ACCOUNT,exp:1700003600})).toString("base64url")+".signature";
 const fetch=vi.fn(async(url:RequestInfo|URL,init:RequestInit={})=>{calls.push({url:String(url),init});return String(url).endsWith("/info")?Response.json({userid:ACCOUNT}):result();});
 const http=new V1Http({scope:gate,journal:{readGate:async()=>state.gate},readToken:async()=>token,now:()=>1700000000000,fetch});
 return {http,calls,state,fetch};
}
describe("authoritative browser owned custom types",()=>{
 test("API principal then exact direct list, no browser cache or aggregate fallback",async()=>{
  const {http,calls}=setup();const result=await readV1OwnedTypes(http);
  expect(calls.map(row=>row.url)).toEqual(["https://api.fulcradynamics.com/user/v1alpha1/info","https://api.fulcradynamics.com/input/v1/data_type?include_deprecated=true"]);
  expect(calls.every(row=>row.init.cache==="no-store"&&row.init.method==="GET")).toBe(true);
  expect(result).toEqual([entry({fulcra_userid:ACCOUNT})]);
  expect(Object.isFrozen(result)&&Object.isFrozen(result[0])&&Object.isFrozen(result[0].record_spec)).toBe(true);
 });
 test("retired types remain visible and a complete empty list is explicit",async()=>{
  const {http}=setup(()=>Response.json([entry({deprecated:true})]));expect((await readV1OwnedTypes(http))[0].deprecated).toBe(true);
  expect(await readV1OwnedTypes(setup(()=>Response.json([])).http)).toEqual([]);
 });
 test.each([null,{},[null],[entry(),entry()],[entry({fulcra_userid:OTHER})],[entry({userid:OTHER})],
  [entry({id:"Event"})],[entry({id:"Event/not-a-uuid"})],[entry({id:`Metric/${OTHER}`})],[entry({api_version:"v1alpha1"})],
  [entry({recordable:false})],[entry({recordable:1})],[entry({deprecated:null})],[entry({deprecated:0})],
  [entry({name:""})],[entry({name:123})],[entry({record_spec:null})]])("unknown protocol refuses the whole list %j",async rows=>{
   const {http,calls}=setup(()=>Response.json(rows));await expect(readV1OwnedTypes(http)).rejects.toThrow();expect(calls).toHaveLength(2);
 });
 test.each([203,204,206,302,401,403,404,429,500])("HTTP%s cannot become an authoritative empty list",async status=>{
  const {http,calls}=setup(()=>new Response(status===204?null:"[]",{status,headers:{"content-type":"application/json"}}));
  await expect(readV1OwnedTypes(http)).rejects.toThrow();expect(calls).toHaveLength(2);expect(calls[1].init.redirect).toBe("error");
 });
 test.each<ResponseInit>([{headers:{"Content-Range":"items 0-0/2"}},{headers:{Link:'</input/v1/data_type?cursor=next>; rel="next"'}},
  {headers:{"X-Next-Cursor":"next"}},{headers:{"X-Has-More":"false"}},{headers:{"X-Total-Count":"2"}},{headers:{"X-Total":"bad"}}])("partial/pagination evidence refuses %j",options=>{
   const {http}=setup(()=>Response.json([],options));return expect(readV1OwnedTypes(http)).rejects.toThrow();
 });
 test("positive matching totals and 4096 unique types are readable; overflow is never truncated",async()=>{
  const rows=Array.from({length:4096},(_,index)=>entry({id:`Event/00000000-0000-4000-8000-${index.toString(16).padStart(12,"0")}`}));
  expect(await readV1OwnedTypes(setup(()=>Response.json(rows,{headers:{"X-Total-Count":"4096"}})).http)).toHaveLength(4096);
  await expect(readV1OwnedTypes(setup(()=>Response.json([...rows,entry()])).http)).rejects.toThrow();
 });
 test("direct type list cannot accept owner overrides, cursor params, or a trailing slash",async()=>{
  const {http,fetch}=setup();
  for(const path of ["/input/v1/data_type/","/input/v1/data_type","/input/v1/data_type?include_deprecated=false","/input/v1/data_type?include_deprecated=true&cursor=x","/input/v1/data_type?include_deprecated=true&fulcra_userid="+ACCOUNT,"/input/v1/data_type?include_deprecated=true&include_deprecated=true"])
   await expect(http.json(path)).rejects.toThrow();
  expect(fetch).not.toHaveBeenCalled();
 });
 test("a generation change during the direct read cannot publish a list",async()=>{
  const {http,state}=setup(()=>{state.gate={...gate,generation:NEXT};return Response.json([entry()]);});await expect(readV1OwnedTypes(http)).rejects.toThrow();
 });
 test("a retargeted provider response cannot claim authoritative scope",async()=>{
  const {http}=setup(()=>{const result=Response.json([]);Object.defineProperty(result,"url",{value:"https://api.fulcradynamics.com/data/v1/catalog"});return result;});
  await expect(readV1OwnedTypes(http)).rejects.toThrow();
 });
});
