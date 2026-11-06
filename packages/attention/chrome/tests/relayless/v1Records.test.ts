// @vitest-environment node
import {describe,expect,test,vi} from "vitest";
import {readProtectionFixture} from "./v1ReadProtectionFixture";
import {V1Http} from "../../src/relayless/v1Http";
import {readV1ExactRecords,v1RecordMatches} from "../../src/relayless/v1Records";
const ACCOUNT="11111111-2222-4333-8444-555555555555",ID="22222222-3333-4444-8555-666666666666",OTHER="33333333-4444-4555-8666-777777777777";
const TYPE="Event/44444444-5555-4666-8777-888888888888",GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const gate={version:1 as const,accountId:ACCOUNT,generation:GEN,enabled:true};
const TOKEN="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":ACCOUNT,exp:1700003600})).toString("base64url")+".signature";
function setup(body:string,options:{status?:number;headers?:Record<string,string>;response?:()=>Response;afterRead?:()=>void;maxResponseBytes?:number;timeoutMs?:number;protection?:ReturnType<typeof readProtectionFixture>|null}={}){
 const protection=options.protection===undefined?readProtectionFixture():options.protection;
 const state={gate:{...gate},token:TOKEN};const paths:string[]=[];
 const http=new V1Http({scope:gate,journal:{readGate:async()=>state.gate},readToken:async()=>state.token,now:()=>1700000000000,maxResponseBytes:options.maxResponseBytes,timeoutMs:options.timeoutMs,readProtection:protection?.protection,
 fetch:async(url,init)=>{expect(init?.method).toBe("GET");paths.push(String(url));if(String(url).endsWith("/info"))return Response.json({userid:ACCOUNT});
 options.afterRead?.();return options.response?.()??new Response(body,{status:options.status??200,headers:{"content-type":"application/x-jsonl",...options.headers}});}});
 return {http,state,paths,protection};
}
describe("bounded exact v1 record identity readback",()=>{
 test.each([[250,25],[25,250]])("exact-reader timeout override caps configured %ims with requested %ims",async(configured,requested)=>{
  vi.useFakeTimers({toFake:["setTimeout","clearTimeout","performance"]});try{
   const s=setup("",{timeoutMs:configured,response:()=>new Response(new ReadableStream<Uint8Array>(),{headers:{"content-type":"application/x-jsonl"}})});
   const failure=expect(readV1ExactRecords(s.http,TYPE,[ID],requested)).rejects.toMatchObject({code:"timeout"});
   await vi.advanceTimersByTimeAsync(26);await failure;expect(s.paths).toHaveLength(2);
  }finally{vi.useRealTimers();}
 });
 test("invalid exact-reader timeout refuses before any provider request",async()=>{
  const s=setup("");await expect(readV1ExactRecords(s.http,TYPE,[ID],0)).rejects.toThrow();expect(s.paths).toEqual([]);
 });
 test("missing network protection refuses before any provider request",async()=>{
  const s=setup("",{protection:readProtectionFixture(false)});await expect(readV1ExactRecords(s.http,TYPE,[ID])).rejects.toThrow();expect(s.paths).toEqual([]);expect(s.protection?.state.writes).toBe(0);
 });
 test("removed protection during a provider callback cannot authorize absence",async()=>{
  const protection=readProtectionFixture(),s=setup("",{protection,afterRead:()=>{protection.state.rules.pop();}});
  await expect(readV1ExactRecords(s.http,TYPE,[ID])).rejects.toThrow();expect(protection.state.writes).toBe(0);
 });
 test("protection removed at body EOF cannot acknowledge a record",async()=>{
  const protection=readProtectionFixture();let step=0;
  const s=setup("",{protection,response:()=>new Response(new ReadableStream({pull(controller){if(step++===0)controller.enqueue(new TextEncoder().encode(JSON.stringify({id:ID})));else{protection.state.rules.pop();controller.close();}}},{highWaterMark:0}),{headers:{"content-type":"application/x-jsonl"}})});
  await expect(readV1ExactRecords(s.http,TYPE,[ID])).rejects.toThrow();
 });
 test.each(["gzip","deflate","br","gzip, identity",""])("visible nonidentity encoding %s refuses despite a complete-looking body",async encoding=>{
  const s=setup(JSON.stringify({id:ID}),{headers:{"content-encoding":encoding}});await expect(readV1ExactRecords(s.http,TYPE,[ID])).rejects.toThrow();
 });
 test("a hung protection assertion spends the same HTTP deadline; late completion cannot fetch",async()=>{
  vi.useFakeTimers();try{
   const protection=readProtectionFixture();let release:((value:typeof protection.state.rules)=>void)|undefined;
   protection.dnr.getDynamicRules=()=>new Promise(resolve=>{release=resolve;});
   const s=setup("",{protection,timeoutMs:25}),failure=expect(readV1ExactRecords(s.http,TYPE,[ID])).rejects.toThrow();
   await vi.advanceTimersByTimeAsync(26);await failure;release!(protection.state.rules);await vi.advanceTimersByTimeAsync(0);expect(s.paths).toEqual([]);
  }finally{vi.useRealTimers();}
 });
 test("builds account-scoped instant OR selectors with the complete destination in every group",async()=>{
  const {http,paths}=setup(JSON.stringify({id:ID,note:"Synthetic"})+"\n"+JSON.stringify({id:OTHER,note:"Other"})+"\n");
  const result=await readV1ExactRecords(http,TYPE,[ID,OTHER]);expect(Object.keys(result)).toEqual([ID,OTHER]);expect(Object.isFrozen(result[ID])).toBe(true);
  const url=new URL(paths[1]);expect(url.pathname).toBe("/data/v1/records");expect(url.searchParams.get("fulcra_userid")).toBe(ACCOUNT);
  expect(url.searchParams.get("q")).toBe(`{__name__="${TYPE}",id="${ID}" or __name__="${TYPE}",id="${OTHER}"}`);
 });
 test.each([
  {userid:OTHER},{fulcra_userid:OTHER},{userid:ACCOUNT,fulcra_userid:OTHER},
  {userid:null},{userid:[]},{userid:1},{fulcra_userid:{}},
 ])("contradictory or malformed exposed owner refuses the entire exact read: %j",async owner=>{
  const {http}=setup(JSON.stringify({id:ID,...owner}));
  await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.toMatchObject({code:"record_readback"});
 });
 test.each([{userid:ACCOUNT},{fulcra_userid:ACCOUNT},{userid:ACCOUNT,fulcra_userid:ACCOUNT}])("matching exposed owner aliases retain exact identity proof: %j",async owner=>{
  const {http}=setup(JSON.stringify({id:ID,...owner}));
  expect((await readV1ExactRecords(http,TYPE,[ID]))[ID]).toEqual({id:ID,...owner});
 });
 test("empty200 stream means no requested identities at this read",async()=>{const {http}=setup("");expect(await readV1ExactRecords(http,TYPE,[ID])).toEqual({});});
 test.each([404,206,500])("HTTP %i cannot become absent",async status=>{const {http}=setup("",{status});await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.toThrow();});
 test.each(["[]\n",'{"id":"'+ID+'"}\nnot-json','{"id":"'+ID+'","value":NaN}','{"id":"'+ID+'","value":"\\ud800"}',
  '{"id":"'+ID+'","value":9007199254740992}',JSON.stringify({id:ID})+"\n"+JSON.stringify({id:ID}),JSON.stringify({id:OTHER})])("malformed/ambiguous response refuses: %s",async body=>{
  const {http}=setup(body);await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.toThrow();
 });
 test.each(['{"id":"'+ID+'","id":"'+ID+'"}','{"id":"'+ID+'","nested":{"a":1,"\\u0061":1}}'])('duplicate keys refuse',async body=>{
  const {http}=setup(body);await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.toThrow();
 });
 test.each(["\u000b","\u000c","\u00a0","\ufeff",JSON.stringify({id:ID})+"\n\u000b"])("invalid whitespace cannot mean absence or hide a malformed suffix",async body=>{
  const {http}=setup(body);await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.toThrow();
 });
 test("ASCII JSONL blank framing may be ignored",async()=>{const {http}=setup(" \t\r\n\t\n");expect(await readV1ExactRecords(http,TYPE,[ID])).toEqual({});});
 test("keys may repeat in separate objects and punctuation inside strings is data",async()=>{
  const {http}=setup(JSON.stringify({id:ID,nested:[{a:"{,:[\""},{a:"}"}]}));expect((await readV1ExactRecords(http,TYPE,[ID]))[ID].id).toBe(ID);
 });
 test.each([{"content-range":"items 0-0/2"},{link:'<https://example.com>; rel="next"'},{"x-total-count":"2"},{"x-fulcra-userid":OTHER}] as Record<string,string>[])("partial or other-account response refuses",async headers=>{
  const {http}=setup(JSON.stringify({id:ID}),{headers});await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.toThrow();
 });
 test("arguments are captured before the principal await",async()=>{
  const {http,paths}=setup(JSON.stringify({id:ID})),ids=[ID];const pending=readV1ExactRecords(http,TYPE,ids);ids[0]=OTHER;
  expect(Object.keys(await pending)).toEqual([ID]);expect(new URL(paths[1]).searchParams.get("q")).toContain(ID);
 });
 test("unsupported identities and oversized batches refuse without fetch",async()=>{
  const {http,paths}=setup("");for(const [type,ids] of [[TYPE,["bad"]],["Event/bad",[ID]],[TYPE,Array(33).fill(ID)]] as [string,string[]][]){await expect(readV1ExactRecords(http,type,ids)).rejects.toThrow();}
  expect(paths).toEqual([]);
 });
 test("late generation change cannot return readback",async()=>{
  const s=setup(JSON.stringify({id:ID}),{afterRead:()=>{s.state.gate={...gate,generation:"bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"};}});
  await expect(readV1ExactRecords(s.http,TYPE,[ID])).rejects.toThrow();
 });
 test("a complete group of32 exact identities is retained",async()=>{
  const ids=Array.from({length:32},(_,i)=>`00000000-0000-4000-8000-${i.toString().padStart(12,"0")}`),body=ids.map(id=>JSON.stringify({id})).join("\n");
  const {http}=setup(body);expect(Object.keys(await readV1ExactRecords(http,TYPE,ids))).toEqual(ids);
 });
 test.each(["oversized","tooManyRows","tooManyLines","wrongMime","redirect","utf8","ownerHeader"])("bounded transport refuses %s",async kind=>{
  let body=JSON.stringify({id:ID}),headers:Record<string,string>={},response:undefined|(()=>Response),maxResponseBytes=1024;
  if(kind==="oversized")body=JSON.stringify({id:ID,note:"x".repeat(2000)});
  if(kind==="tooManyRows")body=Array(33).fill(JSON.stringify({id:ID})).join("\n");
  if(kind==="tooManyLines")body="\n".repeat(4097);
  if(kind==="wrongMime")headers={"content-type":"text/plain"};
  if(kind==="ownerHeader")headers={"x-fulcra-userid":OTHER};
  if(kind==="redirect")response=()=>{const result=new Response(body,{headers:{"content-type":"application/x-jsonl"}});Object.defineProperty(result,"url",{value:"https://example.com/records"});return result;};
  if(kind==="utf8")response=()=>new Response(new Uint8Array([0xff]),{headers:{"content-type":"application/x-jsonl"}});
  if(kind==="tooManyRows"||kind==="tooManyLines")maxResponseBytes=65536;
  const {http}=setup(body,{headers,response,maxResponseBytes});await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.toThrow();
 });
 test("token account change during provider read cannot return data",async()=>{
  const s=setup(JSON.stringify({id:ID}),{afterRead:()=>{s.state.token="synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":OTHER,exp:1700003600})).toString("base64url")+".signature";}});
  await expect(readV1ExactRecords(s.http,TYPE,[ID])).rejects.toThrow();
 });
 test("record caller gets only a static parser failure, with no body fragment",async()=>{
  const {http}=setup("SYNTHETIC_PRIVATE_READBACK");let failure:unknown;try{await readV1ExactRecords(http,TYPE,[ID]);}catch(error){failure=error;}
  expect(failure).toBeInstanceOf(Error);expect((failure as Error).message).not.toContain("SYNTHETIC");
 });
 test("stream failure after a complete-looking row never returns partial success",async()=>{
  let step=0;const {http}=setup("",{response:()=>new Response(new ReadableStream({pull(controller){if(step++===0)controller.enqueue(new TextEncoder().encode(JSON.stringify({id:ID})+"\n"));else controller.error(Error("synthetic private stream error"));}}),{headers:{"content-type":"application/x-jsonl"}})});
  await expect(readV1ExactRecords(http,TYPE,[ID])).rejects.not.toThrow(/synthetic private/);
 });
});
describe("v1 submitted-field comparison",()=>{
 const expected={id:ID,start_time:"2026-01-01T00:00:00.125Z",end_time:"2026-01-01T00:00:01Z",sources:["synthetic"],nested:{a:1,b:null}};
 test("an admitted record larger than1MiB can match exact readback",()=>{
  const large={...expected,note:"x".repeat(1024*1024+1)};
  expect(v1RecordMatches({...large,server_field:true},large)).toBe(true);
 });
 test("the4MiB comparison bound counts UTF8 bytes and holds oversized inputs",()=>{
  const huge={...expected,note:"é".repeat(2*1024*1024)};
  expect(v1RecordMatches(huge,huge)).toBe(false);
 });
 test("equivalent instants and root server fields preserve a match",()=>{expect(v1RecordMatches({...expected,start_time:"2025-12-31T19:00:00.125000-05:00",server_field:true},expected)).toBe(true);});
 test.each([{...expected,nested:{a:true,b:null}},{...expected,nested:{a:1,b:null,extra:true}},{...expected,sources:["changed"]},{...expected,start_time:"2026-01-01T00:00:00.125001Z"},{...expected,start_time:"2026-01-01T00:00:00.125"},{...expected,nested:undefined},null])("changed/missing/invalid submitted values do not match",actual=>{expect(v1RecordMatches(actual,expected)).toBe(false);});
});
