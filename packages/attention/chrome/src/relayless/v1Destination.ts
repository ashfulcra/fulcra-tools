// Opt-in setup binder. It may publish a durable LOCAL binding; it is not a
// read-only preview, capture enablement, record delivery or exclusion proof.
import { V1Http, V1HttpError } from "./v1Http";
import { V1Journal } from "./v1Journal";
import { readV1OwnedTypes, type V1OwnedType } from "./v1OwnedTypes";
import { attentionV1CreateBody, attentionV1Fingerprint, proveAttentionV1Schema, proveAttentionV1InputSchema,
 type AttentionV1SchemaProof } from "./v1AttentionSpec";
const KEY="collect.attention.v1",NAME="Attention";
const EVENT=/^Event\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
interface Options {http:V1Http;journal:V1Journal;pinnedId?:string;allowCreate?:boolean;timeoutMs?:number;signal?:AbortSignal;}
export interface ResolvedAttentionV1 extends AttentionV1SchemaProof {
 readonly accountId:string;readonly generation:string;readonly dataType:string;readonly specFingerprint:string;
 readonly inputSchemaText:string;readonly inputSchemaHash:string;
}
export interface VerifiedAttentionV1 extends ResolvedAttentionV1 {readonly reservationGeneration:string;}
function refuse():never {throw new Error("Attention v1 original destination proof is unavailable; retain the original binding");}
function compatible(row:V1OwnedType):string {
 if(!EVENT.test(row.id)||row.deprecated||row.record_spec.type!=="event"
  ||typeof row.record_spec.schema!=="string"||!row.record_spec.schema)throw new Error("Attention v1 destination is retired or incompatible");
 return row.record_spec.schema;
}
function candidate(rows:readonly V1OwnedType[],pin:string|null):V1OwnedType|null {
 const found=rows.filter(row=>pin?row.id===pin:row.name===NAME);
 if(found.length>1)throw new Error("Attention v1 destinations are ambiguous; choose an explicit owned pin");
 if(!found.length)return null;compatible(found[0]);return found[0];
}
function receiptId(body:unknown,account:string):string {
 if(!body||typeof body!=="object"||Array.isArray(body))throw new Error("Attention v1 creation receipt is unreadable");
 const value=body as Record<string,unknown>;
 if(typeof value.id!=="string"||!EVENT.test(value.id)
  ||(value.fulcra_userid!==undefined&&value.fulcra_userid!==account)
  ||(value.userid!==undefined&&value.userid!==account))throw new Error("Attention v1 creation receipt differs");
 return value.id;
}
function resolve(options:Options,verifyOnly:true):Promise<Readonly<VerifiedAttentionV1>>;
function resolve(options:Options,verifyOnly:false):Promise<Readonly<ResolvedAttentionV1>>;
async function resolve(options:Options,verifyOnly:boolean):Promise<Readonly<ResolvedAttentionV1|VerifiedAttentionV1>> {
 // Capture all caller choices and the HTTP object's immutable authority before
 // hashing or touching durable state. Only explicit setup permits new types.
 const http=options.signal?options.http.withReadCancellation(options.signal):options.http,journal=options.journal,scope=http.scope,explicit=options.pinnedId;
 http.assertJournal(journal);
 const allowCreate=options.allowCreate??false,started=performance.now(),timeoutMs=options.timeoutMs??10000;
 if(!Number.isFinite(started)||!Number.isFinite(timeoutMs)||timeoutMs<=0||timeoutMs>10000)refuse();
 const deadline=started+timeoutMs,cancellation=new AbortController();let ended=false;
 const remaining=()=>{const now=performance.now();if(options.signal?.aborted||ended||!Number.isFinite(now)||now>=deadline)refuse();return deadline-now;};
 const requestBudget=()=>Math.min(8000,remaining());
 const owned=async()=>{const rows=await readV1OwnedTypes(http,requestBudget());remaining();return rows;};
 if(typeof allowCreate!=="boolean"||(explicit!==undefined&&(typeof explicit!=="string"||!EVENT.test(explicit))))throw new Error("Attention v1 destination choice is invalid");
 const operation=async()=>{
 const hash=await attentionV1Fingerprint();remaining();
 const prior=await journal.readCreation(scope,KEY,hash);remaining();
 if(verifyOnly&&prior?.status!=="resolved")refuse();
 const establishedPin=explicit??(prior?.status==="resolved"?prior.destination:null);
 if(explicit&&prior?.destination&&explicit!==prior.destination)throw new Error("Attention v1 original destination cannot be replaced");
 let pin=explicit??prior?.destination??null;
 let selected=candidate(await owned(),pin);
 if(!selected) {
  if(pin||!allowCreate)throw new Error("Attention v1 owned destination is unavailable; keep the original binding");
  const reserved=await journal.reserveCreation(scope,KEY,hash,deadline,options.signal??cancellation.signal);remaining();
  if(reserved.intent.destination) {
   pin=reserved.intent.destination;
  }else {
   if(!reserved.created)throw new Error("Attention v1 prior creation remains unresolved; reconcile it without another POST");
   let receipt:unknown,failed:unknown;
   try{receipt=await http.json("/input/v1/data_type/Event",{method:"POST",body:attentionV1CreateBody(),timeoutMs:requestBudget()});}
   catch(error){failed=error;}
   if(failed===undefined) {
    pin=receiptId(receipt,scope.accountId!);
    await journal.retainCreationReceipt(scope,KEY,hash,pin);
   }
   // Production may return a redirect after committing creation, and fetch
   // rejects it. Unknown dispatch is reconciled ONLY with explicit fresh reads;
   // an empty result never clears the intent or authorizes another create.
   remaining();selected=candidate(await owned(),pin);
   if(!selected){if(failed instanceof V1HttpError)throw failed;throw new Error("Attention v1 creation remains unresolved; keep its durable intent");}
  }
  if(!selected)selected=candidate(await owned(),pin);
  if(!selected)throw new Error("Attention v1 original destination is not visible");
 }
 remaining();const inputSchema=compatible(selected),dataType=selected.id;
 const inputProof=await proveAttentionV1InputSchema(inputSchema);remaining();
 const served=await http.json(`/data/v1/catalog/${dataType}/v1/schema`,{timeoutMs:requestBudget()});remaining();
 const proof=await proveAttentionV1Schema(JSON.stringify(served));remaining();
 // The input schema includes internal transforms redacted from the served
 // schema. Compare its own captured text across reads, not unlike byte forms.
 const final=candidate(await owned(),establishedPin?dataType:null);
 if(!final||final.id!==dataType||compatible(final)!==inputSchema)throw new Error("Attention v1 destination changed during schema proof");
 remaining();if(!verifyOnly){await journal.reserveCreation(scope,KEY,hash,deadline,options.signal??cancellation.signal);remaining();}
 const bound=verifyOnly?await journal.requireCreation(scope,KEY,hash,dataType,prior!.createdUnderGeneration)
  :await journal.resolveCreation(scope,KEY,hash,dataType,deadline,options.signal??cancellation.signal);remaining();
 return Object.freeze({...proof,inputSchemaText:inputProof.inputSchemaText,inputSchemaHash:inputProof.inputSchemaHash,
  accountId:scope.accountId!,generation:scope.generation,dataType,specFingerprint:hash,
  ...verifyOnly?{reservationGeneration:bound.createdUnderGeneration}:{}});
 };
 let timer:ReturnType<typeof setTimeout>|undefined;
 const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{ended=true;cancellation.abort();reject(new Error("Attention v1 destination proof timed out; retain original work"));},Math.ceil(remaining()));});
 try{return await Promise.race([operation(),timeout]);}finally{ended=true;cancellation.abort();clearTimeout(timer);}
}
export function resolveAttentionV1(options:Options):Promise<Readonly<ResolvedAttentionV1>> {return resolve(options,false);}
/** Fresh proof of existing original authority; no setup/adoption/local writes. */
export function verifyAttentionV1(options:Pick<Options,"http"|"journal"|"pinnedId"|"timeoutMs"|"signal">):Promise<Readonly<VerifiedAttentionV1>> {
 return resolve({http:options.http,journal:options.journal,pinnedId:options.pinnedId,timeoutMs:options.timeoutMs,signal:options.signal,allowCreate:false},true);
}
