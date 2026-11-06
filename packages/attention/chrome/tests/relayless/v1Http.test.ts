// @vitest-environment node
import { describe, expect, test, vi } from "vitest";
import { V1Http } from "../../src/relayless/v1Http";
import type { V1Gate } from "../../src/relayless/v1Journal";
const ACCOUNT="11111111-2222-4333-8444-555555555555", OTHER="22222222-3333-4444-8555-666666666666";
const GENERATION="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", NEXT="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
const gate:V1Gate={version:1,accountId:ACCOUNT,generation:GENERATION,enabled:true};
const NOW=1_700_000_000_000;
function token(account=ACCOUNT,marker="initial",exp=NOW/1000+3600) {
 return "synthetic."+Buffer.from(JSON.stringify({"fulcradynamics.com/userid":account,exp,marker})).toString("base64url")+".signature";
}
function setup(handler:(url:string,init:RequestInit)=>Response|Promise<Response>=()=>Response.json({ok:true}),
 options:{scope?:V1Gate;timeoutMs?:number;maxResponseBytes?:number}={}) {
 const state={gate:{...gate} as V1Gate|null,token:token() as string|null};
 const calls:{url:string;init:RequestInit}[]=[];
 const fetchFn=vi.fn(async(url:RequestInfo|URL,init:RequestInit={})=>{
  calls.push({url:String(url),init});
  return String(url).endsWith("/info")?Response.json({userid:ACCOUNT}):handler(String(url),init);
 }) as typeof fetch;
 const http=new V1Http({scope:options.scope??gate,journal:{readGate:async()=>state.gate},readToken:async()=>state.token,
  fetch:fetchFn,now:()=>NOW,timeoutMs:options.timeoutMs,maxResponseBytes:options.maxResponseBytes});
 return {http,state,calls,fetchFn};
}
describe("captured browser v1 HTTP authority",()=>{
 for(const path of ["/data/v1/records?q=synthetic","/data/v1/records/","/data/v1/%72ecords?q=synthetic",
  "/data/v1/records%2f?q=synthetic","/data/v1/catalog%2F..%2Frecords?q=synthetic",
  "/data/v1/catalog/Event%2f"+OTHER+"/v1/schema","/data/v1/other"])test("generic JSON cannot bypass typed records protection: "+path,async()=>{
  const {http,fetchFn}=setup(()=>new Response('{"id":"'+OTHER+'"}',{
   headers:{"content-type":"application/json","content-encoding":"gzip"}}));
  await expect(http.json(path)).rejects.toMatchObject({code:"route"});expect(fetchFn).not.toHaveBeenCalled();
 });
 for(const path of ["/data/v1/catalog","/data/v1/catalog/Event","/data/v1/catalog/DeletedRecord/v1/schema",
  "/data/v1/catalog/Event/"+OTHER+"/v1/schema","/data/v1/catalog/Metric/"+OTHER])test("canonical catalog JSON remains available: "+path,async()=>{
  const {http,calls}=setup();expect(await http.json(path)).toEqual({ok:true});expect(calls).toHaveLength(2);
 });
 for(const kind of ["redirected","retargeted"])test("wrong-scope404 cannot become target absence: "+kind,async()=>{
  const {http}=setup(()=>{const response=new Response("",{status:404});
   Object.defineProperty(response,kind==="redirected"?"redirected":"url",{value:kind==="redirected"?true:"https://api.fulcradynamics.com/data/v1/catalog"});return response;});
  await expect(http.json("/user/v1alpha1/tag/name/attention")).rejects.toMatchObject({code:"response_scope"});
 });
 test("API identity proof precedes a v1 request with isolated auth and no redirects",async()=>{
  const {http,calls}=setup();expect(await http.json("/data/v1/catalog")).toEqual({ok:true});
  expect(calls.map(row=>row.url)).toEqual(["https://api.fulcradynamics.com/user/v1alpha1/info","https://api.fulcradynamics.com/data/v1/catalog"]);
  for(const {init} of calls){expect(init.redirect).toBe("error");expect(init.credentials).toBe("omit");
   expect(new Headers(init.headers).get("authorization")).toBe("Bearer "+token());expect(init.signal).toBeInstanceOf(AbortSignal);}
 });
 test("JWT candidate cannot replace API principal proof",async()=>{
  const {http,calls,fetchFn}=setup();vi.mocked(fetchFn).mockImplementationOnce(async()=>Response.json({userid:OTHER}));
  await expect(http.json("/data/v1/catalog")).rejects.toThrow();expect(calls).toHaveLength(0);
 });
 for(const value of [null,"bad",token(OTHER),token(ACCOUNT,"expired",1),token(ACCOUNT,"invalid",NaN)])test("invalid token refuses before fetch: "+String(value).slice(0,8),async()=>{
  const {http,state,fetchFn}=setup();state.token=value;await expect(http.json("/data/v1/catalog")).rejects.toThrow();expect(fetchFn).not.toHaveBeenCalled();
 });
 test("same-account replacement requires a new independently proved operation",async()=>{
  const {http,state,calls,fetchFn}=setup();const renewed=token(ACCOUNT,"renewed");
  vi.mocked(fetchFn).mockImplementationOnce(async()=>{state.token=renewed;return Response.json({userid:ACCOUNT});});
  await expect(http.json("/data/v1/catalog")).rejects.toMatchObject({code:"unauthorized"});expect(calls).toHaveLength(0);
  await http.json("/data/v1/catalog");expect(new Headers(calls[1].init.headers).get("authorization")).toBe("Bearer "+renewed);
 });
 for(const change of ["generation","account","signedout","disabled"])test("scope change during proof blocks POST: "+change,async()=>{
  const {http,state,fetchFn}=setup();vi.mocked(fetchFn).mockImplementationOnce(async()=>{
   if(change==="generation")state.gate={...gate,generation:NEXT};if(change==="account")state.token=token(OTHER);
   if(change==="signedout")state.token=null;if(change==="disabled")state.gate={...gate,enabled:false};return Response.json({userid:ACCOUNT});
  });
  await expect(http.json("/input/v1/data_type/Event",{method:"POST",body:{name:"synthetic"}})).rejects.toThrow();expect(fetchFn).toHaveBeenCalledTimes(1);
 });
 test("generation change during POST retains the unacknowledged result",async()=>{
  const {http,state,calls}=setup(()=>{state.gate={...gate,generation:NEXT};return Response.json({id:OTHER});});
  await expect(http.json("/input/v1/data_type/Event",{method:"POST",body:{name:"synthetic"}})).rejects.toThrow();expect(calls).toHaveLength(2);
 });
 test("disabled capture allows setup read but cannot ingest",async()=>{
  const disabled={...gate,enabled:false};const {http,state,fetchFn}=setup(undefined,{scope:disabled});state.gate=disabled;
  await http.json("/data/v1/catalog");vi.mocked(fetchFn).mockClear();
  await expect(http.json("/ingest/v1/record/Event/"+OTHER,{method:"POST",body:[]})).rejects.toThrow();expect(fetchFn).not.toHaveBeenCalled();
 });
 test("capture body and scope before first await",async()=>{
  const input={nested:{name:"original"}},scope={...gate};const {http,calls}=setup(undefined,{scope});
  const pending=http.json("/input/v1/data_type/Event",{method:"POST",body:input});input.nested.name="changed";scope.accountId=OTHER;
  await pending;expect(JSON.parse(String(calls[1].init.body))).toEqual({nested:{name:"original"}});
 });
 for(const body of [{value:NaN},{value:Infinity},{value:undefined},{value:"\ud800"},{value:9007199254740992},new Date()])test("unsupported/lossy request JSON refuses",async()=>{
  const {http,fetchFn}=setup();await expect(http.json("/input/v1/data_type/Event",{method:"POST",body})).rejects.toThrow();expect(fetchFn).not.toHaveBeenCalled();
 });
 test("request accessor is not executed",async()=>{
  const getter=vi.fn(()=>"private"),body=Object.defineProperty({},"value",{enumerable:true,get:getter}),{http,fetchFn}=setup();
  await expect(http.json("/input/v1/data_type/Event",{method:"POST",body})).rejects.toThrow();expect(getter).not.toHaveBeenCalled();expect(fetchFn).not.toHaveBeenCalled();
 });
 for(const path of ["https://example.com/data/v1/catalog","//example.com/data/v1/catalog","/data/v1/catalog#secret","/data/v1/catalog/../record","/user/v1alpha1/annotation","/data/v1/catalog?fulcra_userid="+OTHER])test("route refuses before fetch: "+path,async()=>{
  const {http,fetchFn}=setup();await expect(http.json(path)).rejects.toThrow();expect(fetchFn).not.toHaveBeenCalled();
 });
 for(const status of [401,403,422,500,302])test("HTTP failure never retries POST: "+status,async()=>{
  const {http,calls}=setup(()=>new Response("synthetic-private-provider-body",{status}));
  await expect(http.json("/input/v1/data_type/Event",{method:"POST",body:{name:"synthetic"}})).rejects.not.toThrow(/synthetic-private/);expect(calls).toHaveLength(2);
 });
 test("decoded response cap refuses, never empty-success",async()=>{
  const {http}=setup(()=>Response.json({value:"x".repeat(300)}),{maxResponseBytes:128});await expect(http.json("/data/v1/catalog")).rejects.toThrow();
 });
 test("hung fetch times out and aborts",async()=>{
  vi.useFakeTimers({toFake:["setTimeout","clearTimeout","performance"]});try{const {http,calls}=setup(()=>new Promise(()=>{}),{timeoutMs:25});
   const failure=expect(http.json("/input/v1/data_type/Event",{method:"POST",body:{name:"synthetic"}})).rejects.toThrow();
   await vi.advanceTimersByTimeAsync(26);await failure;expect(calls).toHaveLength(2);expect(calls[1].init.signal?.aborted).toBe(true);
  }finally{vi.useRealTimers();}
 });
});

test("published scope cannot be replaced while a request awaits authority",async()=>{
 const {http,calls}=setup();const pending=http.json("/data/v1/catalog");
 expect(Reflect.set(http,"scope",{...gate,accountId:OTHER})).toBe(false);
 await pending;expect(calls).toHaveLength(2);expect(http.scope.accountId).toBe(ACCOUNT);
});

test("request size budget stops walking an amplified shared-string payload early",async()=>{
 const reached=vi.fn(()=>Object.prototype);
 const tail=new Proxy({}, {getPrototypeOf:reached});
 const body=Array.from({length:2000},()=>"x".repeat(1024));
 const input:unknown[]=[...body,tail];const {http,fetchFn}=setup();
 await expect(http.json("/input/v1/data_type/Event",{method:"POST",body:input})).rejects.toThrow();
 expect(reached).not.toHaveBeenCalled();expect(fetchFn).not.toHaveBeenCalled();
});

test("timeout while gate is unreadable cannot dispatch a late POST",async()=>{
 vi.useFakeTimers({toFake:["setTimeout","clearTimeout","performance"]});try{
  let resume!:(value:V1Gate)=>void;const fetchFn=vi.fn() as typeof fetch;
  const http=new V1Http({scope:gate,journal:{readGate:()=>new Promise(resolve=>{resume=resolve;})},
   readToken:async()=>token(),fetch:fetchFn,now:()=>NOW,timeoutMs:25});
  const failure=expect(http.json("/input/v1/data_type/Event",{method:"POST",body:{name:"synthetic"}})).rejects.toThrow();
  await vi.advanceTimersByTimeAsync(26);await failure;resume(gate);await Promise.resolve();await Promise.resolve();
  expect(fetchFn).not.toHaveBeenCalled();
 }finally{vi.useRealTimers();}
});

test("a hung response body is cancelled at the whole-request deadline",async()=>{
 vi.useFakeTimers({toFake:["setTimeout","clearTimeout","performance"]});try{
  const cancel=vi.fn();const body=new ReadableStream<Uint8Array>({pull:()=>new Promise(()=>{}),cancel});
  const {http}=setup(()=>new Response(body,{headers:{"Content-Type":"application/json"}}),{timeoutMs:25});
  const failure=expect(http.json("/data/v1/catalog")).rejects.toThrow();
  await vi.advanceTimersByTimeAsync(26);await failure;expect(cancel).toHaveBeenCalled();
 }finally{vi.useRealTimers();}
});

for(const body of [new Uint8Array([255]),new TextEncoder().encode('{"value":"\\ud800"}'),
 new TextEncoder().encode('{"value":1e400}'),new TextEncoder().encode('{"value":9007199254740993}')]) {
 test("invalid or lossy response data is never published",async()=>{
  const {http}=setup(()=>new Response(body,{headers:{"Content-Type":"application/json"}}));
  await expect(http.json("/data/v1/catalog")).rejects.toThrow();
 });
}

test("elapsed budget blocks HTTP even when microtasks delay the timeout callback",async()=>{
 const fetchFn=vi.fn(async()=>Response.json({userid:ACCOUNT})) as typeof fetch;
 const http=new V1Http({scope:gate,journal:{readGate:async()=>gate},readToken:async()=>{
  const until=performance.now()+25;while(performance.now()<until){/* synchronous browser work */}return token();
 },fetch:fetchFn,now:()=>NOW,timeoutMs:5});
 await expect(http.json("/input/v1/data_type/Event",{method:"POST",body:{name:"synthetic"}})).rejects.toThrow();
 expect(fetchFn).not.toHaveBeenCalled();
});

test("a captured per-operation timeout bounds a proof read",async()=>{
 vi.useFakeTimers({toFake:["setTimeout","clearTimeout","performance"]});try{
  const {http,calls}=setup(()=>new Promise(()=>{}),{timeoutMs:250});
  const options={timeoutMs:25};const pending=http.json("/data/v1/catalog",options);options.timeoutMs=250;
  const failure=expect(pending).rejects.toMatchObject({code:"timeout"});
  await vi.advanceTimersByTimeAsync(26);await failure;expect(calls).toHaveLength(2);expect(calls[1].init.signal?.aborted).toBe(true);
 }finally{vi.useRealTimers();}
});
test("per-operation timeout cannot extend the configured transport budget",async()=>{
 vi.useFakeTimers({toFake:["setTimeout","clearTimeout","performance"]});try{
  const {http}=setup(()=>new Promise(()=>{}),{timeoutMs:25});
  const failure=expect(http.json("/data/v1/catalog",{timeoutMs:250})).rejects.toMatchObject({code:"timeout"});
  await vi.advanceTimersByTimeAsync(26);await failure;
 }finally{vi.useRealTimers();}
});
for(const timeoutMs of [0,-1,NaN,Infinity,30001])test("invalid operation timeout refuses before provider: "+timeoutMs,async()=>{
 const {http,fetchFn}=setup();await expect(http.json("/data/v1/catalog",{timeoutMs})).rejects.toThrow();expect(fetchFn).not.toHaveBeenCalled();
});
test("generic schema JSON cannot silently discard duplicate constraints",async()=>{
 const {http}=setup(()=>new Response('{"properties":{"schema_version":{"const":2,"const":1}}}',{
  headers:{"content-type":"application/json"}}));
 await expect(http.json("/data/v1/catalog/DeletedRecord/v1/schema")).rejects.toMatchObject({code:"invalid_json"});
});
