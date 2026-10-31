// Opt-in browser transport. Generic JSON permits setup, never record ingest.
// Typed effect submission re-proves original schema/identity and owns one CAS;
// auth publication, replay policy, exact delivery and settlement remain actors.
// Browser fetch exposes decoded bytes: this is a retained-body cap, NOT a
// raw-wire or browser-engine decompression memory guarantee.
import { API_BASE } from "./config";
import {V1ReadProtection} from "./v1ReadProtection";
import type { FetchFn } from "./oidc";
import type { V1Gate,V1Journal,V1MutationIntent,V1DispatchAuthority } from "./v1Journal";

const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const catalogPath=/^\/data\/v1\/catalog(?:\/(?:[A-Za-z][A-Za-z0-9_]{0,127}|(?:Event|Metric)\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(?:\/v1\/schema)?)?$/;
const encoder=new TextEncoder();
const partialHeaders=["content-range","link","x-next-cursor","x-next-page","x-has-more",
  "x-continuation-token","next-cursor","next-page","x-page","x-page-size","x-limit","x-offset"];
export class V1HttpError extends Error {
  constructor(readonly code:string, readonly status?:number) {
    super("Attention v1 request could not be verified. Keep the queued data and try again.");
    this.name="V1HttpError";
  }
}
function fail(code:string):never {throw new V1HttpError(code);}
function elapsed(signal:AbortSignal,deadline:number):void {
  const current=performance.now();
  if(signal.aborted||!Number.isFinite(current)||current>=deadline)fail("timeout");
}
/** Reject JSON.stringify's silent coercions before any await or request. */
export function snapshotV1JSON(value:unknown,maxBytes:number):unknown {
  const stack=new Set<object>();let nodes=0,bytes=0;
  const spend=(count:number)=>{bytes+=count;if(bytes>maxBytes)fail("body_limit");};
  const stringSize=(text:string)=>{
    spend(2);
    for(let i=0;i<text.length;i++) {
      const code=text.charCodeAt(i);
      if(code>=0xd800&&code<=0xdbff) {
        const next=text.charCodeAt(++i);
        if(!(next>=0xdc00&&next<=0xdfff))fail("invalid_json");
        spend(4);
      }else if(code>=0xdc00&&code<=0xdfff)fail("invalid_json");
      else if(code<32)spend([8,9,10,12,13].includes(code)?2:6);
      else if(code===34||code===92)spend(2);
      else spend(code<128?1:code<2048?2:3);
    }
  };
  const visit=(item:unknown,depth:number):unknown=>{
    if(++nodes>100000||depth>64)fail("invalid_json");
    if(item===null||typeof item==="boolean"){spend(item===false?5:4);return item;}
    if(typeof item==="string") {
      if(item.length>maxBytes)fail("invalid_json");stringSize(item);return item;
    }
    if(typeof item==="number") {
      if(!Number.isFinite(item)||(Number.isInteger(item)&&!Number.isSafeInteger(item)))fail("invalid_json");
      spend(String(item).length);return item;
    }
    if(!item||typeof item!=="object"||stack.has(item)||Object.getOwnPropertySymbols(item).length)fail("invalid_json");
    const array=Array.isArray(item);
    if(!array&&![Object.prototype,null].includes(Object.getPrototypeOf(item)))fail("invalid_json");
    const keys=Object.getOwnPropertyNames(item);
    if(array&&(item.length>100000||keys.length!==item.length+1))fail("invalid_json");
    spend(2+Math.max(0,(array?item.length:keys.length)-1));
    stack.add(item);
    const result:unknown[]=[],record:Record<string,unknown>=Object.create(null);
    for(const key of keys) {
      if(array&&key==="length")continue;
      if(!array){stringSize(key);spend(1);}
      const descriptor=Object.getOwnPropertyDescriptor(item,key);
      if(!descriptor?.enumerable||!Object.hasOwn(descriptor,"value"))fail("invalid_json");
      if(array) {
        if(!/^(0|[1-9][0-9]*)$/.test(key)||Number(key)>=item.length)fail("invalid_json");
        result[Number(key)]=visit(descriptor.value,depth+1);
      }else record[key]=visit(descriptor.value,depth+1);
    }
    stack.delete(item);return array?result:record;
  };
  const result=visit(value,0);
  if(encoder.encode(JSON.stringify(result)).length>maxBytes)fail("body_limit");
  return result;
}
/** Parse bounded JSON and reject duplicate keys, including escaped aliases.
 * JSON.parse establishes grammar first; the token walk only tracks key scopes.
 * Its failures are always static and never expose retained provider text. */
export function parseV1JSON(text:string):unknown {
  let result:unknown;try{result=JSON.parse(text);}catch{return fail("invalid_json");}
  const tokens=/"(?:[^"\\]|\\[\s\S])*"|[{}\[\],:]/g;
  const stack:{object:boolean;key:boolean;seen:Set<string>}[]=[];let match:RegExpExecArray|null,count=0;
  while((match=tokens.exec(text))) {
    // Punctuation/keys consume multiple tokens per JSON value. The separate
    // snapshot walk still bounds values at100,000 (including owned inventories).
    if(++count>400000)fail("invalid_json");const token=match[0],top=stack.at(-1);
    if(token==="{"||token==="["){if(stack.length>=64)fail("invalid_json");stack.push({object:token==="{",key:token==="{",seen:new Set()});}
    else if(token==="}"||token==="]")stack.pop();
    else if(token===","){if(top?.object)top.key=true;}
    else if(token.startsWith('"')&&top?.object&&top.key) {
      const key=JSON.parse(token) as string;if(top.seen.has(key))fail("invalid_json");top.seen.add(key);top.key=false;
    }
  }
  return result;
}
function captureGate(value:unknown):V1Gate {
  const gate=snapshotV1JSON(value,1024) as V1Gate;
  if(!gate||typeof gate!=="object"||Object.keys(gate).length!==4||gate.version!==1
    ||typeof gate.accountId!=="string"||!UUID.test(gate.accountId)||typeof gate.generation!=="string"
    ||!UUID.test(gate.generation)||typeof gate.enabled!=="boolean")fail("scope_invalid");
  return Object.freeze({...gate});
}
function principal(token:unknown,now:number):string {
  if(typeof token!=="string"||token.length>65536||!Number.isFinite(now)
    ||!/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token))fail("unauthorized");
  try {
    const payload=token.split(".")[1].replace(/-/g,"+").replace(/_/g,"/");
    const bytes=Uint8Array.from(atob(payload+"=".repeat((4-payload.length%4)%4)),char=>char.charCodeAt(0));
    const claims=snapshotV1JSON(JSON.parse(new TextDecoder("utf-8",{fatal:true}).decode(bytes)),65536) as Record<string,unknown>;
    const account=claims["fulcradynamics.com/userid"],exp=claims.exp;
    if(typeof account!=="string"||!UUID.test(account)||typeof exp!=="number"||!Number.isFinite(exp)
      ||!Number.isFinite(exp*1000)||exp*1000<=now)fail("unauthorized");
    return account;
  }catch {return fail("unauthorized");}
}
interface Options {
  scope:V1Gate;
  journal:Pick<import("./v1Journal").V1Journal,"readGate">;
  // Read-only, fresh token lookup. A future auth actor owns any renewal and
  // must publish its new generation before changing the capture account.
  readToken:()=>Promise<string|null>;
  fetch?:FetchFn;
  now?:()=>number;
  timeoutMs?:number;
  maxResponseBytes?:number;
  readProtection?:V1ReadProtection;
  lifetime?:Readonly<{signal:AbortSignal;deadline:number}>;
}
export interface V1EffectSubmission {journal:V1Journal;intent:V1MutationIntent;name:string;authority:V1DispatchAuthority;timeoutMs?:number;signal?:AbortSignal;}
interface ReadSession {readonly token:string;readonly deadline:number;readonly signal:AbortSignal;readonly transportSignal:AbortSignal;readonly effectEpoch:number;active:boolean;}
interface RequestOptions {method?:"GET"|"POST";body?:unknown;timeoutMs?:number;}
export class V1Http {
  readonly #scope:Readonly<V1Gate>;
  get scope():Readonly<V1Gate> {return this.#scope;}
  readonly #journal:Options["journal"];
  private readonly readToken:Options["readToken"];
  private readonly fetchFn:FetchFn;
  private readonly now:()=>number;
  private readonly timeoutMs:number;
  private readonly maxResponseBytes:number;
  readonly #readProtection:V1ReadProtection|undefined;
  #readCancellation:readonly AbortSignal[]=[];
  #root={effectEpoch:0};
  readonly #lifetime:Options["lifetime"];
  #readSession:ReadSession|undefined;
  constructor(options:Options) {
    this.#lifetime=options.lifetime?Object.freeze({...options.lifetime}):undefined;
    if(this.#lifetime&&(!Number.isFinite(this.#lifetime.deadline)||this.#lifetime.deadline-performance.now()>30000))fail("configuration");
    this.#readProtection=options.readProtection;
    this.#scope=captureGate(options.scope);this.#journal=options.journal;this.readToken=options.readToken;
    this.fetchFn=options.fetch??((...args:Parameters<FetchFn>)=>fetch(...args));this.now=options.now??Date.now;
    this.timeoutMs=options.timeoutMs??15000;this.maxResponseBytes=options.maxResponseBytes??4*1024*1024;
    if(!Number.isFinite(this.timeoutMs)||this.timeoutMs<=0||this.timeoutMs>30000
      ||!Number.isSafeInteger(this.maxResponseBytes)||this.maxResponseBytes<64||this.maxResponseBytes>16*1024*1024)fail("configuration");
  }
  /** Local effects and HTTP proof must use one authority, never two gates
   * that merely happen to contain equal values at construction time. */
  assertJournal(journal:object):void {
    if(journal!==this.#journal)fail("journal_scope");
  }
  private async guard(signal:AbortSignal,deadline:number,expected?:string):Promise<string> {
    const session=this.#readSession;
    const alive=()=>{
      elapsed(signal,deadline);if(this.#lifetime)elapsed(this.#lifetime.signal,this.#lifetime.deadline);for(const inherited of this.#readCancellation)if(inherited.aborted)fail("timeout");
      if(session){if(!session.active||session.effectEpoch!==this.#root.effectEpoch)fail("decision_expired");elapsed(session.signal,session.deadline);elapsed(session.transportSignal,session.deadline);}
    };
    const check=async()=>{
      alive();const current=captureGate(await this.#journal.readGate());alive();
      if(current.accountId!==this.#scope.accountId||current.generation!==this.#scope.generation||current.enabled!==this.#scope.enabled)fail("scope_changed");
    };
    try{
      if(session)expected=session.token;
      await check();const token=await this.readToken();alive();
      if(principal(token,this.now())!==this.#scope.accountId||expected!==undefined&&token!==expected)fail("unauthorized");
      await check();return token!;
    }catch(error){if(session)session.active=false;throw error;}
  }
  private async readJSON(response:Response,signal:AbortSignal,deadline:number,lines=false,token?:string):Promise<unknown> {
    const mime=lines?/^application\/x-jsonl(?:\s*;|$)/i:/^application\/(?:[a-z0-9.+-]+\+)?json(?:\s*;|$)/i;
    if(!response.body||!mime.test(response.headers.get("content-type")??""))fail("invalid_json");
    const reader=response.body.getReader();let size=0;
    const chunks:Uint8Array[]=[];
    const cancel=()=>{void reader.cancel().catch(()=>{});};
    signal.addEventListener("abort",cancel,{once:true});
    try {
      while(true) {
        elapsed(signal,deadline);
        const {done,value}=await reader.read();
        elapsed(signal,deadline);
        await this.guard(signal,deadline,token);
        if(done)break;
        size+=value.byteLength;if(size>this.maxResponseBytes)fail("body_limit");
        chunks.push(value.slice());
      }
      const body=new Uint8Array(size);let offset=0;
      for(const chunk of chunks){body.set(chunk,offset);offset+=chunk.length;}
      elapsed(signal,deadline);
      // Preserve a JSONL BOM so invalid non-JSON framing cannot disappear.
      const text=new TextDecoder("utf-8",{fatal:true,ignoreBOM:lines}).decode(body);
      let parsed:unknown;
      if(lines) {
        const rows:unknown[]=[];let offset=0,lineCount=0;
        while(offset<text.length) {
          if(++lineCount>4096)fail("invalid_json");
          const newline=text.indexOf("\n",offset),end=newline<0?text.length:newline;
          const line=text.slice(offset,end);offset=end+1;
          if(!/^[ \t\r]*$/.test(line)) {if(rows.length>=32)fail("invalid_json");rows.push(parseV1JSON(line));}
        }
        parsed=rows;
      }else parsed=parseV1JSON(text);
      const result=snapshotV1JSON(parsed,this.maxResponseBytes);
      elapsed(signal,deadline);return result;
    }finally {
      signal.removeEventListener("abort",cancel);
      cancel();reader.releaseLock();
    }
  }
  private async request(url:URL,method:"GET"|"POST",body:string|undefined,signal:AbortSignal,deadline:number,lines=false,protection?:V1ReadProtection,expected?:string):Promise<unknown> {
    if(lines&&!protection)fail("read_protection");
    if(protection)await protection.assert();
    const token=await this.guard(signal,deadline,expected);
    elapsed(signal,deadline);if(method==="POST")this.#root.effectEpoch++;
    const response=await this.fetchFn(url.href,{method,body,redirect:"error",credentials:"omit",cache:"no-store",signal,
      headers:{Authorization:"Bearer "+token,Accept:lines?"application/x-jsonl":"application/json",...(body===undefined?{}:{"Content-Type":"application/json"})}});
    try {
      await this.guard(signal,deadline,token);
      if(response.redirected||response.type==="opaqueredirect"||(response.url&&response.url!==url.href))fail("response_scope");
      if(!response.ok)throw new V1HttpError("http_error",response.status);
      if(method==="GET"&&response.status!==200)fail("incomplete_response");
      const ownedTypes=url.pathname==="/input/v1/data_type";
      if((ownedTypes||lines)&&partialHeaders.some(name=>response.headers.has(name)))fail("incomplete_response");
      const owner=response.headers.get("x-fulcra-userid");
      if(lines&&owner!==null&&owner!==this.#scope.accountId)fail("response_scope");
      if(lines&&(response.headers.get("content-encoding")??"identity").trim().toLowerCase()!=="identity")fail("encoded_response");
      if(protection){await protection.assert();await this.guard(signal,deadline,token);}
      const result=await this.readJSON(response,signal,deadline,lines,token);
      if(ownedTypes||lines) {
        if(!Array.isArray(result))fail("incomplete_response");
        for(const name of ["x-total-count","x-total"]) {
          const total=response.headers.get(name);
          if(total!==null&&(!/^[0-9]{1,10}$/.test(total)||Number(total)!==result.length))fail("incomplete_response");
        }
      }
      if(protection)await protection.assert();
      await this.guard(signal,deadline,token);return result;
    }finally {if(response.body&&!response.body.locked)void response.body.cancel().catch(()=>{});}
  }
  json(path:string,options:RequestOptions={}):Promise<unknown> {
    return this.execute(path,options);
  }
  /** Preserve this exact transport authority/configuration while narrowing
   * nested reads to all inherited cancellation signals. No new deadline or
   * dispatch permission is granted; callers still pass their remaining budget. */
  withReadCancellation(signal:AbortSignal):V1Http {
    const copy=new V1Http({scope:this.#scope,journal:this.#journal,readToken:this.readToken,fetch:this.fetchFn,now:this.now,
      timeoutMs:this.timeoutMs,maxResponseBytes:this.maxResponseBytes,readProtection:this.#readProtection,lifetime:this.#lifetime});
    copy.#root=this.#root;copy.#readSession=this.#readSession;
    copy.#readCancellation=Object.freeze([...new Set([...this.#readCancellation,signal])]);
    return copy;
  }
  /** A bounded read-only principal session. This proves only the exact bearer,
   * never metadata, policy, typed effects or local settlement. A and B callers
   * explicitly renew the server proof at their authority boundaries. */
  async withPrincipalReads<T>(deadline:number,signal:AbortSignal,read:(http:V1Http)=>Promise<T>):Promise<T> {
    if(this.#readSession||!Number.isFinite(deadline)||deadline-performance.now()>30000)fail("decision_scope");
    const controller=new AbortController(),copy=this.withReadCancellation(signal).withReadCancellation(controller.signal);
    const inherited=[...new Set([...this.#readCancellation,signal])],cancel=()=>controller.abort();
    for(const external of inherited)external.addEventListener("abort",cancel,{once:true});if(inherited.some(external=>external.aborted))cancel();
    let session:ReadSession|undefined,timer:ReturnType<typeof setTimeout>|undefined;
    const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new V1HttpError("timeout"));},Math.max(0,deadline-performance.now()));});
    const cancelled=new Promise<never>((_,reject)=>{controller.signal.addEventListener("abort",()=>reject(new V1HttpError("timeout")),{once:true});});
    const operation=async()=>{
      const token=await copy.guard(signal,deadline);
      session={token,deadline,signal,transportSignal:controller.signal,effectEpoch:this.#root.effectEpoch,active:true};copy.#readSession=session;
      await copy.proveReadPrincipal(deadline,signal);const result=await read(copy);await copy.assertFresh(deadline,signal);return result;
    };
    try{return await Promise.race([operation(),timeout,cancelled]);}
    finally{if(session)session.active=false;clearTimeout(timer);for(const external of inherited)external.removeEventListener("abort",cancel);controller.abort();}
  }
  /** Identity comparison, not a shape-equal authority substitute. */
  sameReadAuthority(other:V1Http):boolean {return this.#root===other.#root&&this.#journal===other.#journal&&this.#readSession===other.#readSession&&!!this.#readSession?.active;}
  /** Captures local exact-token continuity only; this is no server/schema
   * proof and cannot authorize HTTP, effects or settlement on its own. */
  async captureLocalGuard(deadline:number,signal:AbortSignal):Promise<()=>Promise<void>> {
    const token=await this.guard(signal,deadline);
    return async()=>{await this.guard(signal,deadline,token);};
  }
  async assertFresh(deadline:number,signal:AbortSignal):Promise<void> {
    const session=this.#readSession;
    if(session&&(deadline>session.deadline||signal!==session.signal))fail("decision_scope");
    await this.guard(signal,deadline);
  }
  async proveReadPrincipal(deadline:number,signal:AbortSignal):Promise<void> {
    const session=this.#readSession;if(!session||!session.active||deadline>session.deadline||signal!==session.signal)fail("decision_scope");
    const boundedDeadline=Math.min(deadline,performance.now()+this.timeoutMs),controller=new AbortController(),http=this.withReadCancellation(controller.signal);
    const inherited=[...new Set([...this.#readCancellation,session.transportSignal])],cancel=()=>controller.abort();
    for(const external of inherited)external.addEventListener("abort",cancel,{once:true});if(inherited.some(external=>external.aborted))cancel();
    let timer:ReturnType<typeof setTimeout>|undefined;
    const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new V1HttpError("timeout"));},Math.max(0,boundedDeadline-performance.now()));});
    const cancelled=new Promise<never>((_,reject)=>{controller.signal.addEventListener("abort",()=>reject(new V1HttpError("timeout")),{once:true});});
    try{
      const proof=await Promise.race([http.request(new URL("/user/v1alpha1/info",API_BASE),"GET",undefined,controller.signal,boundedDeadline,false,undefined,session.token),timeout,cancelled]);
      if(!proof||typeof proof!=="object"||Array.isArray(proof)||(proof as Record<string,unknown>).userid!==this.#scope.accountId)fail("unauthorized");
      await this.assertFresh(deadline,signal);
    }catch(error){session.active=false;throw error;}
    finally{clearTimeout(timer);for(const external of inherited)external.removeEventListener("abort",cancel);controller.abort();}
  }
  /** One-use receipt transport only. No replay permission, delivery proof,
   * native absence, logical verification or queued-row acknowledgement. */
  async submitEffect(options:V1EffectSubmission):Promise<Readonly<Record<string,unknown>>> {
    const started=performance.now(),journal=options.journal,name=options.name,external=AbortSignal.any([...this.#readCancellation,...this.#lifetime?[this.#lifetime.signal]:[],...options.signal?[options.signal]:[]]);
    this.assertJournal(journal);if(this.#readSession)fail("decision_read_only");
    const intent=snapshotV1JSON(options.intent,512*1024) as unknown as V1MutationIntent;
    const authority=snapshotV1JSON(options.authority,65536) as unknown as V1DispatchAuthority;
    const requested=options.timeoutMs??this.timeoutMs;
    if(!Number.isFinite(started)||!Number.isFinite(requested)||requested<=0||requested>30000
      ||typeof name!=="string"||!["deliver","repair","policy","delete"].includes(name)||external?.aborted)fail("effect_scope");
    const timeoutMs=Math.min(requested,this.timeoutMs),deadline=Math.min(started+timeoutMs,this.#lifetime?.deadline??Infinity);
    const controller=new AbortController(),signal=controller.signal;let ended=false;
    const check=()=>{if(ended)fail("timeout");elapsed(signal,deadline);if(this.#lifetime)elapsed(this.#lifetime.signal,this.#lifetime.deadline);};
    const protection=this.#readProtection??new V1ReadProtection();
    const forward=()=>controller.abort();external?.addEventListener("abort",forward,{once:true});
    if(external?.aborted)controller.abort();
    let timer:ReturnType<typeof setTimeout>|undefined;
    const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{ended=true;controller.abort();reject(new V1HttpError("timeout"));},Math.max(0,Math.ceil(deadline-performance.now())));});
    const cancel=new Promise<never>((_,reject)=>{signal.addEventListener("abort",()=>reject(new V1HttpError("timeout")),{once:true});});
    const responseScope=(response:Response,url:URL,statuses:number[])=>{
      check();if(response.redirected||response.type==="opaqueredirect"||(response.url&&response.url!==url.href))fail("response_scope");
      if(!statuses.includes(response.status))throw new V1HttpError("http_error",response.status);
      if(partialHeaders.some(header=>response.headers.has(header)))fail("incomplete_response");
      const owner=response.headers.get("x-fulcra-userid");if(owner!==null&&owner!==this.#scope.accountId)fail("response_scope");
      if((response.headers.get("content-encoding")??"identity").trim().toLowerCase()!=="identity")fail("encoded_response");
    };
    // Pin and server-prove this exact bearer BEFORE the once-only CAS. The
    // winning CAS is the last authority await before fetch, with no token
    // lookup/substitution after it. A committed issue is never reset.
    const provePrincipal=async(token:string)=>{
      check();if(principal(token,this.now())!==this.#scope.accountId)fail("unauthorized");
      const url=new URL("/user/v1alpha1/info",API_BASE);
      const response=await this.fetchFn(url.href,{method:"GET",redirect:"error",credentials:"omit",cache:"no-store",signal,headers:{Authorization:"Bearer "+token,Accept:"application/json"}});
      try{await this.guard(signal,deadline,token);check();responseScope(response,url,[200]);const body=await this.readJSON(response,signal,deadline,false,token);check();
       if(!body||typeof body!=="object"||Array.isArray(body)||(body as Record<string,unknown>).userid!==this.#scope.accountId)fail("unauthorized");
      }finally{if(response.body&&!response.body.locked)void response.body.cancel().catch(()=>{});}
    };
    const operation=async()=>{
      const fresh=await this.captureLocalGuard(deadline,signal);check();
      await protection.assert();check();await fresh();
      const {prepareV1TypedEffect}=await import("./v1TypedEffect");check();await fresh();
      const effect=await prepareV1TypedEffect(this,journal,intent,name,authority,deadline,signal);check();await fresh();
      const token=await this.guard(signal,deadline);check();await provePrincipal(token);check();await protection.assert();check();await this.guard(signal,deadline,token);check();await fresh();
      const url=new URL("/ingest/v1/record/"+effect.destination+"?api_version=v1",API_BASE);
      if(!await journal.issueGuardedEffect(this.#scope,intent,name,authority,deadline,signal))fail("effect_already_issued");
      check();if(principal(token,this.now())!==this.#scope.accountId)fail("unauthorized");
      this.#root.effectEpoch++;
      const response=await this.fetchFn(url.href,{method:"POST",body:effect.bodyText,redirect:"error",credentials:"omit",cache:"no-store",signal,
        headers:{Authorization:"Bearer "+token,Accept:"application/json","Content-Type":"application/x-jsonl"}});
      try{
        await this.guard(signal,deadline,token);check();responseScope(response,url,[200,201,202]);
        const receipt=await this.readJSON(response,signal,deadline,false,token);check();
        if(!receipt||typeof receipt!=="object"||Array.isArray(receipt))fail("effect_receipt");
        const row=receipt as Record<string,unknown>,upload=row.upload_id;
        if(typeof upload!=="string"||!upload.trim()||[...upload].length>256||/[\x00-\x1f\x7f]/.test(upload)
          ||["userid","fulcra_userid"].some(key=>Object.hasOwn(row,key)&&row[key]!==this.#scope.accountId))fail("effect_receipt");
        await protection.assert();check();await this.guard(signal,deadline,token);check();await provePrincipal(token);check();await this.guard(signal,deadline,token);check();
        return Object.freeze(row);
      }finally{if(response.body&&!response.body.locked)void response.body.cancel().catch(()=>{});}
    };
    try{const result=await Promise.race([operation(),timeout,cancel]);check();return result;}
    catch(error){throw error instanceof V1HttpError?error:new V1HttpError("effect_failed");}
    finally{ended=true;clearTimeout(timer);external?.removeEventListener("abort",forward);controller.abort();}
  }
  /** Exact, bounded instant-ID selectors only; no caller-supplied query/account. */
  async records(dataType:string,identifiers:readonly string[],timeoutMs?:number):Promise<unknown> {
    const ids=snapshotV1JSON(identifiers,2048);
    if(typeof dataType!=="string"||!/^(Event|Metric)\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(dataType)
      ||!Array.isArray(ids)||!ids.length||ids.length>32||ids.some(id=>typeof id!=="string"||!UUID.test(id)))fail("record_scope");
    const query="{"+[...new Set(ids)].map(id=>'__name__='+JSON.stringify(dataType)+',id='+JSON.stringify(id)).join(" or ")+"}";
    const params=new URLSearchParams({q:query,fulcra_userid:this.#scope.accountId!});
    return this.execute("/data/v1/records?"+params.toString(),{timeoutMs},true);
  }
  private async execute(path:string,options:RequestOptions={},lines=false):Promise<unknown> {
    // The absolute monotonic budget includes synchronous capture work too;
    // expired timers may be starved by promise microtasks or browser work.
    const started=performance.now(),requestedTimeout=options.timeoutMs??this.timeoutMs;
    if(!Number.isFinite(requestedTimeout)||requestedTimeout<=0||requestedTimeout>30000)fail("configuration");
    const timeoutMs=Math.min(requestedTimeout,this.timeoutMs),deadline=Math.min(started+timeoutMs,this.#lifetime?.deadline??Infinity);
    // All caller-owned inputs are captured before the first await.
    const method=options.method??"GET";if(this.#readSession&&method!=="GET")fail("decision_read_only");
    if(typeof path!=="string"||!path.startsWith("/")||path.startsWith("//")||path.length>16384)fail("route");
    const url=new URL(path,API_BASE);
    const ownedTypes=method==="GET"&&url.pathname==="/input/v1/data_type";
    // JSON routes cannot reach records, even through encoded path aliases.
    // Only records() may select the bounded JSONL/protected transport leg.
    const allowed=method==="GET"?(lines?url.pathname==="/data/v1/records":
      ownedTypes||catalogPath.test(url.pathname)||url.pathname.startsWith("/user/v1alpha1/tag/name/")
       ||(/^\/user\/v1alpha1\/tag\/id\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(url.pathname)&&!url.search))
      :method==="POST"&&(url.pathname.startsWith("/input/v1/data_type/")||url.pathname==="/user/v1alpha1/tag");
    const owners=url.searchParams.getAll("fulcra_userid");
    if(!allowed||url.origin!==API_BASE||url.hash||path.split("?")[0]!==url.pathname
      ||(owners.length>0&&(owners.length!==1||owners[0]!==this.#scope.accountId)))fail("route");
    if(ownedTypes&&JSON.stringify([...url.searchParams])!=='[["include_deprecated","true"]]')fail("route");
    if(url.pathname.startsWith("/ingest/")&&!this.#scope.enabled)fail("scope_changed");
    if((method==="GET"&&options.body!==undefined)||(method==="POST"&&options.body===undefined))fail("invalid_json");
    const body=method==="POST"?JSON.stringify(snapshotV1JSON(options.body,1024*1024)):undefined;
    const controller=new AbortController(),external=[...this.#readCancellation,...this.#lifetime?[this.#lifetime.signal]:[]];let timer:ReturnType<typeof setTimeout>|undefined;
    const forward=()=>controller.abort();
    const cancel=new Promise<never>((_,reject)=>{controller.signal.addEventListener("abort",()=>reject(new V1HttpError("timeout")),{once:true});});
    for(const signal of external)signal.addEventListener("abort",forward,{once:true});
    if(external.some(signal=>signal.aborted))controller.abort();
    const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{controller.abort();reject(new V1HttpError("timeout"));},Math.max(0,deadline-performance.now()));});
    const operation=async()=>{
      elapsed(controller.signal,deadline);
      const protection=lines?(this.#readProtection??new V1ReadProtection()):undefined;
      if(protection)await protection.assert();
      const token=await this.guard(controller.signal,deadline);
      let proof:Record<string,unknown>;
      try{proof=this.#readSession?{userid:this.#scope.accountId}:await this.request(new URL("/user/v1alpha1/info",API_BASE),"GET",undefined,controller.signal,deadline,false,undefined,token) as Record<string,unknown>;}
      catch(error){
        // A failed principal endpoint is never evidence that the requested
        // catalog/tag/record is absent (especially an identity-check 404).
        if(error instanceof V1HttpError&&error.code==="http_error")throw new V1HttpError("principal_http_error",error.status);
        throw error;
      }
      if(!proof||Array.isArray(proof)||proof.userid!==this.#scope.accountId)fail("unauthorized");
      return this.request(url,method,body,controller.signal,deadline,lines,protection,token);
    };
    try {
      const result=await Promise.race([operation(),timeout,cancel]);
      elapsed(controller.signal,deadline);return result;
    }
    catch(error) {if(this.#readSession)this.#readSession.active=false;throw error instanceof V1HttpError?error:new V1HttpError("request_failed");}
    finally {clearTimeout(timer);for(const signal of external)signal.removeEventListener("abort",forward);controller.abort();}
  }
}
