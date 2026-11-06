// Internal bounded authority observations. Evidence is as-of-read: catalogue and
// record equality cannot detect provider ABA or make these endpoints atomic.
import {V1Http,snapshotV1JSON,parseV1JSON} from './v1Http';
import {effectPlans} from './v1EffectBodies';
import type {V1Journal,V1MutationIntent,V1DispatchAuthority} from './v1Journal';
import {readV1OwnedTypes,type V1OwnedType} from './v1OwnedTypes';
import {attentionV1Fingerprint,proveAttentionV1InputSchema,proveAttentionV1Schema} from './v1AttentionSpec';
import {exclusionV1Contract,exclusionV1Fingerprint,proveExclusionV1InputSchema,proveExclusionV1Schema} from './v1ExclusionSpec';
import {attentionV1Contract} from './v1Wire';
import {proveV1NativeDeletionSchema,prepareV1TypedEffect} from './v1TypedEffect';
import {readV1Exclusions} from './v1ExclusionPolicy';
import {readV1ExactRecords,v1RecordMatches} from './v1Records';
import {buildV1ExclusionRecord} from './v1Exclusions';
import {sha256Hex} from './sha256';
export interface V1AuthorityEvidence {target:{schemaHash:string;inputSchemaHash:string};control:{schemaHash:string;inputSchemaHash:string};nativeSchemaHash:string;}
interface Options {http:V1Http;journal:V1Journal;intent:V1MutationIntent;authority:V1DispatchAuthority;target:string;control:string;deadline:number;signal:AbortSignal;}
// The type is deliberately empty: only identity in this module's WeakMap counts.
export interface V1DecisionCapability {readonly opaque?:never;}
interface State {options:Options;evidence:V1AuthorityEvidence;recordId:string;authorityText:string;active:boolean;cancellation:AbortController;}
const proofs=new WeakMap<object,State>();
/** A caught helper failure must still revoke the actual decision, including
 * descendants and the outer callback's final publication check. Undefined or
 * forged proofs grant nothing and cannot revoke a different real capability. */
export async function withV1DecisionFailure<T>(proof:V1DecisionCapability|undefined,action:()=>Promise<T>):Promise<T> {
 try{return await action();}catch(error){
  const state=proof&&proofs.get(proof);
  if(proof&&state){state.active=false;proofs.delete(proof);state.cancellation.abort();}
  throw error;
 }
}
function hold():never {throw Error('Original v1 decision authority unavailable; retain work');}
export function stableV1Authority(value:unknown):string {if(Array.isArray(value))return '['+value.map(stableV1Authority).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+stableV1Authority((value as Record<string,unknown>)[key])).join(',')+'}';return JSON.stringify(value);}
function selected(rows:readonly V1OwnedType[],id:string):V1OwnedType {const row=rows.find(r=>r.id===id);if(!row||row.deprecated||row.record_spec.type!=='event'||typeof row.record_spec.schema!=='string'||!row.record_spec.schema)hold();return row;}
function check(options:Options):void {if(options.signal.aborted||!Number.isFinite(options.deadline)||performance.now()>=options.deadline)hold();options.http.assertJournal(options.journal);}
async function seam<T>(options:Options,action:()=>Promise<T>):Promise<T> {check(options);await options.http.assertFresh(options.deadline,options.signal);const value=await action();check(options);await options.http.assertFresh(options.deadline,options.signal);return value;}
/** Only a complete successful closure can mint the callback's capability. */
async function snapshot(options:Options):Promise<V1AuthorityEvidence> {
 const cancellation=new AbortController(),http=options.http.withReadCancellation(cancellation.signal);
 const {journal,authority,target,control,signal}=options,deadline=Math.min(options.deadline,performance.now()+8000),o={...options,http,deadline};
 const operation=async():Promise<V1AuthorityEvidence>=>{
 const remaining=()=>{check(o);return Math.min(8000,deadline-performance.now());};
 if(!http.scope.enabled||options.intent.accountId!==http.scope.accountId||options.intent.generation!==http.scope.generation||!Array.isArray(authority.creations)||authority.creations.length!==2||!Array.isArray(authority.tags)||authority.tags.length>128||target===control)hold();
 const a=authority.creations.find(row=>row.specKey===attentionV1Contract().key),c=authority.creations.find(row=>row.specKey===exclusionV1Contract().key);
 if(!a||!c||a.destination!==target||c.destination!==control)hold();
 const ah=await seam(o,attentionV1Fingerprint),ch=await seam(o,exclusionV1Fingerprint);
 if(ah!==a.fingerprint||ch!==c.fingerprint)hold();
 await seam(o,()=>journal.requireCreation(http.scope,a.specKey,ah,target,a.createdUnderGeneration));
 await seam(o,()=>journal.requireCreation(http.scope,c.specKey,ch,control,c.createdUnderGeneration));
 const opening=await seam(o,()=>readV1OwnedTypes(http,remaining()));
 const ar=selected(opening,target),cr=selected(opening,control),atext=ar.record_spec.schema as string,ctext=cr.record_spec.schema as string;
 const ai=await seam(o,()=>proveAttentionV1InputSchema(atext)),ci=await seam(o,()=>proveExclusionV1InputSchema(ctext));
 const servedA=await seam(o,()=>http.json(`/data/v1/catalog/${target}/v1/schema`,{timeoutMs:remaining()}));
 const ap=await seam(o,()=>proveAttentionV1Schema(JSON.stringify(servedA)));
 const servedC=await seam(o,()=>http.json(`/data/v1/catalog/${control}/v1/schema`,{timeoutMs:remaining()}));
 const cp=await seam(o,()=>proveExclusionV1Schema(JSON.stringify(servedC)));
 for(const tag of authority.tags){
  if(typeof tag.destination!=='string'||!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(tag.destination))hold();
  const value=await seam(o,()=>http.json('/user/v1alpha1/tag/id/'+tag.destination,{timeoutMs:remaining()}));
  if(!value||typeof value!=='object'||Array.isArray(value))hold();const row=value as Record<string,unknown>;
  const owners=['userid','fulcra_userid'].filter(key=>Object.hasOwn(row,key));
  if(row.id!==tag.destination||row.deleted_at!==undefined&&row.deleted_at!==null||!owners.length||owners.some(key=>row[key]!==http.scope.accountId))hold();
 }
 const native=await seam(o,()=>http.json('/data/v1/catalog/DeletedRecord/v1/schema',{timeoutMs:remaining()}));proveV1NativeDeletionSchema(native);
 const nativeSchemaHash=await seam(o,()=>sha256Hex(stableV1Authority(native)));
 const closing=await seam(o,()=>readV1OwnedTypes(http,remaining()));
 if(selected(closing,target).record_spec.schema!==atext||selected(closing,control).record_spec.schema!==ctext)hold();
 await seam(o,()=>journal.requireCreation(http.scope,a.specKey,ah,target,a.createdUnderGeneration));
 await seam(o,()=>journal.requireCreation(http.scope,c.specKey,ch,control,c.createdUnderGeneration));
 check(o);await http.assertFresh(deadline,signal);
 return Object.freeze({target:Object.freeze({schemaHash:ap.schemaHash,inputSchemaHash:ai.inputSchemaHash}),control:Object.freeze({schemaHash:cp.schemaHash,inputSchemaHash:ci.inputSchemaHash}),nativeSchemaHash});
 };
 let timer:ReturnType<typeof setTimeout>|undefined;
 const timeout=new Promise<never>((_,reject)=>{timer=setTimeout(()=>{cancellation.abort();reject(Error("Original v1 authority snapshot timed out"));},Math.max(0,deadline-performance.now()));});
 try{return await Promise.race([operation(),timeout]);}finally{clearTimeout(timer);cancellation.abort();}
}
/** Validate the actual opaque proof and all original bindings. No JSON, equal
 * shaped HTTP instance, wider deadline or different cancellation can inherit it. */
export async function requireV1DecisionCapability(proof:V1DecisionCapability,http:V1Http,journal:V1Journal,deadline:number,signal:AbortSignal,intent?:V1MutationIntent,authority?:V1DispatchAuthority):Promise<Readonly<{evidence:V1AuthorityEvidence;target:string;control:string;recordId:string}>> {
 return withV1DecisionFailure(proof,async()=>{
 const state=proofs.get(proof);if(!state||!state.active)hold();const o=state.options;
 if(!o.http.sameReadAuthority(http)||journal!==o.journal||deadline>o.deadline||signal!==o.signal||intent&&(["version","accountId","generation","actionId","operation","requestText","requestHash","requestBytes","ordinal"].some(key=>intent[key as keyof V1MutationIntent]!==o.intent[key as keyof V1MutationIntent]))||authority&&stableV1Authority(authority)!==state.authorityText)hold();
 await seam({...o,http,deadline,signal},async()=>undefined);
 return Object.freeze({evidence:state.evidence,target:o.target,control:o.control,recordId:state.recordId});
 });
}
/** Fresh complete snapshot scoped to one callback/decision. */
export async function withV1DecisionAuthority<T>(options:Options,action:(http:V1Http,proof:V1DecisionCapability,evidence:V1AuthorityEvidence)=>Promise<T>):Promise<T> {
 const captured={...options,intent:Object.freeze(snapshotV1JSON(options.intent,2*1024*1024)) as unknown as V1MutationIntent,authority:Object.freeze(snapshotV1JSON(options.authority,65536)) as unknown as V1DispatchAuthority};check(captured);
 const first=effectPlans(captured.intent)[0],body=first.bodyText?parseV1JSON(first.bodyText) as Record<string,unknown>:null;
 if(captured.intent.operation==='deliver'?first.destination!==captured.target:first.destination!==captured.control||body?.target_data_type!==captured.target)hold();
 const recordId=first.bodyRef?.recordId??(captured.intent.operation==='deliver'?body?.id:body?.target_record_id);
 if(typeof recordId!=='string'||!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(recordId))hold();
 const cancellation=new AbortController();
 try{return await captured.http.withReadCancellation(cancellation.signal).withPrincipalReads(captured.deadline,captured.signal,async http=>{
  const o={...captured,http},evidence=await snapshot(o),proof=Object.freeze({}),state:State={options:o,evidence,recordId,authorityText:stableV1Authority(o.authority),active:true,cancellation};proofs.set(proof,state);
  try{const value=await action(http,proof,evidence);await requireV1DecisionCapability(proof,http,o.journal,o.deadline,o.signal,o.intent,o.authority);return value;}
  finally{state.active=false;proofs.delete(proof);}
 });}finally{cancellation.abort();}
}
/** One coherent A / current policy+source / B decision. Both snapshots have
 * their own fresh principal and complete opening/closing catalogue pair. */
export async function observeV1Decision(options:Options&{recordId:string;payload:Record<string,unknown>;baseline?:V1AuthorityEvidence;validateOriginal?:boolean}):Promise<Readonly<{evidence:V1AuthorityEvidence;policyHash:string|null;source:Readonly<Record<string,unknown>>|null}>> {
 const captured={...options,payload:snapshotV1JSON(options.payload,4*1024*1024) as Record<string,unknown>,baseline:options.baseline?snapshotV1JSON(options.baseline,8192) as unknown as V1AuthorityEvidence:undefined};
 return withV1DecisionAuthority(captured,async(http,proof,a)=>{
  const o={...captured,http};if(captured.baseline&&stableV1Authority(a)!==stableV1Authority(captured.baseline))hold();
  if(captured.validateOriginal)await seam(o,()=>prepareV1TypedEffect(http,o.journal,o.intent,o.intent.operation==='deliver'?'deliver':'policy',o.authority,o.deadline,o.signal,proof));
  const read=await seam(o,()=>readV1Exclusions({http,journal:o.journal,targetDataType:o.target,recordIds:[captured.recordId],timeoutMs:Math.min(10000,o.deadline-performance.now()),decision:{proof,deadline:o.deadline,signal:o.signal}}));
  const row=read.exclusions[captured.recordId];let policyHash:string|null=null;
  if(row){const canonical=await seam(o,()=>buildV1ExclusionRecord({controlType:o.control,targetDataType:o.target,targetRecordId:captured.recordId,actionId:row.action_id as string,requestedAt:row.start_time as string,reason:row.reason as string,evidence:row.evidence as string[]}));policyHash=await seam(o,()=>sha256Hex(stableV1Authority(canonical)));}
  const rows=await seam(o,()=>readV1ExactRecords(http,o.target,[captured.recordId],Math.min(8000,o.deadline-performance.now()))),source=rows[captured.recordId]??null;
  if(o.intent.operation==='deliver'&&source&&!v1RecordMatches(source,captured.payload))hold();
  await seam(o,()=>http.proveReadPrincipal(o.deadline,o.signal));
  const b=await snapshot(o);if(stableV1Authority(a)!==stableV1Authority(b))hold();
  return Object.freeze({evidence:b,policyHash,source});
 });
}
