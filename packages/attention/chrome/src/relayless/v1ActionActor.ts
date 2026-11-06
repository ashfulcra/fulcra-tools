// Opt-in reconciliation of one ORIGINAL reserved action. Observations are as-of-read;
// they never acknowledge a queue row, release ownership or prove backend quiescence.
import {V1Http,parseV1JSON,snapshotV1JSON} from './v1Http';
import {V1Journal,type V1MutationIntent,type V1DispatchAuthority} from './v1Journal';
import {effectPlans} from './v1EffectBodies';
import {v1RecordMatches} from './v1Records';
import {observeV1Decision} from './v1DecisionAuthority';
import {buildV1ExclusionRecord} from './v1Exclusions';
import {attentionV1Contract} from './v1Wire';
import {exclusionV1Contract} from './v1ExclusionSpec';
import {sha256Hex} from './sha256';
interface Options {http:V1Http;journal:V1Journal;intent:V1MutationIntent;timeoutMs?:number;signal?:AbortSignal;}
type Outcome='present'|'already-excluded'|'not-needed'|'absent';
interface Schema {schemaHash:string;inputSchemaHash:string;}
interface Context {accountId:string;generation:string;actionId:string;operation:'deliver'|'exclude';requestHash:string;phase:string;targetDataType:string;targetRecordId:string;controlDataType:string;authorityHash:string;payloadHash:string;}
interface Proof {version:1;outcome:Outcome;evidence:{actor:'collect-v1-action-actor';version:1;context:Context;target:Schema;control:Schema;policyHash:string|null;nativeSchemaHash:string;};}
interface OriginalObservation {intent:V1MutationIntent;targetDataType:string;targetRecordId:string;controlDataType:string;bodyText:string;bodyHash:string;dispatchAuthority:V1DispatchAuthority;}
export interface V1ActionObservation {readonly version:1;readonly status:'observed'|'unknown';readonly asOfRead:true;readonly ownerHeld:true|null;readonly actorNeverReleased:true;readonly accountId:string;readonly generation:string;readonly actionId:string;readonly operation:'deliver'|'exclude';readonly outcome:'present'|'excluded'|'unknown';readonly original:Readonly<OriginalObservation>|null;readonly phaseProofs:readonly Proof[];readonly currentEvidence:Readonly<{target:Schema;control:Schema;policyHash:string|null;nativeSchemaHash:string}>|null;}
// Only the actual final actor path can mint this capability. Exported validation
// consumes identity, never accepts serialized shapes or caller booleans.
const observations=new WeakMap<object,{journal:V1Journal;deadline:number}>();
export function consumeV1ActionObservation(value:Readonly<V1ActionObservation>,journal:V1Journal,deadline:number):boolean {
 const capability=observations.get(value);observations.delete(value);
 return !!capability&&capability.journal===journal&&Number.isFinite(deadline)&&deadline<=capability.deadline&&performance.now()<deadline;
}
function hold():never{throw new Error('Original v1 action is unknown; retain all work');}
function freeze<T>(value:T):T{if(value&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;}
function exact(value:unknown,keys:string[]):value is Record<string,unknown>{return !!value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));}
function stable(value:unknown):string{if(Array.isArray(value))return '['+value.map(stable).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+stable((value as Record<string,unknown>)[key])).join(',')+'}';return JSON.stringify(value);}
const allowed:Record<string,Outcome[]>={deliver:['present','already-excluded'],policy:['already-excluded','present'],repair:['not-needed','absent'],delete:['absent']};
/** Every error is UNKNOWN. In particular a receipt failure is reconciled by an
 * exact read, never by retrying an issued physical phase. */
export async function reconcileV1Action(options:Options):Promise<Readonly<V1ActionObservation>> {
 const started=performance.now(),originalHttp=options.http,journal=options.journal,timeoutMs=options.timeoutMs??30000,external=options.signal;
 const controller=new AbortController(),signal=controller.signal,deadline=started+timeoutMs;
 const http=originalHttp.withReadCancellation(signal);
 let original:V1MutationIntent;
 try{original=freeze(snapshotV1JSON(options.intent,2*1024*1024) as unknown as V1MutationIntent);}catch{return freeze({version:1,status:'unknown',asOfRead:true,ownerHeld:null,actorNeverReleased:true,accountId:'',generation:'',actionId:'',operation:'deliver',outcome:'unknown',original:null,phaseProofs:[],currentEvidence:null});}
 const base={version:1 as const,asOfRead:true as const,actorNeverReleased:true as const,accountId:original.accountId,generation:original.generation,actionId:original.actionId,operation:original.operation};
 const unknown=()=>freeze({...base,status:'unknown' as const,ownerHeld:null,outcome:'unknown' as const,original:null,phaseProofs:[],currentEvidence:null});
 const cancel=()=>controller.abort();external?.addEventListener('abort',cancel,{once:true});if(external?.aborted)cancel();
 const remaining=()=>{const now=performance.now();if(signal.aborted||!Number.isFinite(now)||now>=deadline)hold();return deadline-now;};
 let timer:ReturnType<typeof setTimeout>|undefined;
 const operation=async():Promise<Readonly<V1ActionObservation>>=>{
  if(!Number.isFinite(started)||!Number.isFinite(timeoutMs)||timeoutMs<=0||timeoutMs>30000)hold();remaining();http.assertJournal(journal);
  const fresh=await http.captureLocalGuard(deadline,signal);
  const checked=async()=>{remaining();await fresh();remaining();};
  if(original.version!==2||original.accountId!==http.scope.accountId||original.generation!==http.scope.generation||original.status==='verified')hold();
  const request=parseV1JSON(original.requestText) as {details?:{dispatchAuthority?:V1DispatchAuthority}};
  const authority=freeze(snapshotV1JSON(request.details?.dispatchAuthority,65536) as unknown as V1DispatchAuthority);
  const plans=effectPlans(original),first=plans[0],native=plans[1];
  const primary=await journal.readEffect(http.scope,original,first.name,deadline,signal);await checked();
  const payload=parseV1JSON(primary.bodyText) as Record<string,unknown>;
  const target=original.operation==='deliver'?first.destination:payload.target_data_type as string;
  const recordId=original.operation==='deliver'?payload.id as string:payload.target_record_id as string;
  const control=authority.creations.find(row=>row.specKey===exclusionV1Contract().key)?.destination;
  if(!control||!authority.creations.some(row=>row.specKey===attentionV1Contract().key&&row.destination===target))hold();
  const authorityHash=await sha256Hex(stable(authority));await checked();
  let current=await journal.readMutation(http.scope,original.actionId,deadline,signal);await checked();
  if(!current||current.requestText!==original.requestText||current.status==='verified')hold();
  const decisionOptions={http,journal,intent:original,authority,target,control,recordId,payload,deadline,signal};
  const firstDecision=await observeV1Decision({...decisionOptions,validateOriginal:true});await checked();
  const initial=firstDecision.evidence;
  const observe=async()=>{const decision=await observeV1Decision({...decisionOptions,baseline:initial});await checked();return decision;};
  const ctx=(phase:string):Context=>({accountId:original.accountId,generation:original.generation,actionId:original.actionId,operation:original.operation,requestHash:original.requestHash,phase,targetDataType:target,targetRecordId:recordId,controlDataType:control,authorityHash,payloadHash:plans.find(p=>p.name===phase)!.bodyHash});
  const retained=new Map<string,Proof>();
  for(const effect of current.effects??[]){if(effect.status!=='verified')continue;
   const p=parseV1JSON(effect.proofText??'') as unknown as Proof;
   if(!exact(p,['version','outcome','evidence'])||p.version!==1||!allowed[effect.name]?.includes(p.outcome)
    ||!exact(p.evidence,['actor','version','context','target','control','policyHash','nativeSchemaHash'])||p.evidence.actor!=='collect-v1-action-actor'||p.evidence.version!==1
    ||stable(p.evidence.context)!==stable(ctx(effect.name))||p.evidence.nativeSchemaHash!==initial.nativeSchemaHash||stable(p.evidence.target)!==stable(initial.target)||stable(p.evidence.control)!==stable(initial.control)
    ||!(p.evidence.policyHash===null||typeof p.evidence.policyHash==='string'&&/^[a-f0-9]{64}$/.test(p.evidence.policyHash)))hold();
   if(effect.name==='deliver'&&p.outcome==='present'&&p.evidence.policyHash!==null)hold();
   if((p.outcome==='already-excluded'||effect.name==='policy'&&p.outcome==='present'||p.outcome==='absent')&&p.evidence.policyHash===null)hold();
   if(p.outcome==='not-needed'&&p.evidence.policyHash!==null)hold();
   retained.set(effect.name,p);
  }
  const refresh=async()=>{current=await journal.readMutation(http.scope,original.actionId,deadline,signal);await checked();if(!current||current.requestText!==original.requestText||current.status==='verified')hold();return current;};
  const persist=async(name:string,outcome:Outcome,policyHash:string|null)=>{
   remaining();const proof:Proof={version:1,outcome,evidence:{actor:'collect-v1-action-actor',version:1,context:ctx(name),...initial,policyHash}};
   const old=retained.get(name);if(old&&stable(old)!==stable(proof))hold();
   current=await journal.verifyGuardedEffect(http.scope,original,name,authority,proof,deadline,signal);await checked();retained.set(name,proof);
  };
  const submit=async(name:string)=>{
   remaining();try{await http.submitEffect({journal,intent:original,name,authority,timeoutMs:remaining(),signal});}catch{remaining();}
   remaining();await refresh();
  };
  let policyHash=firstDecision.policyHash,observed=firstDecision.source;await checked();
  if(original.operation==='deliver'){
   const historic=retained.get(first.name);
   if(historic?.outcome==='already-excluded'){
    if(policyHash!==historic.evidence.policyHash)hold();
   }else if(historic?.outcome==='present'){
    if(!policyHash&&!observed)hold();if(observed&&!v1RecordMatches(observed,payload))hold();
   }else if(policyHash){
    await refresh();
    if(current!.effects![0].status==='prepared')await persist(first.name,'already-excluded',policyHash);
    else {if(!observed||!v1RecordMatches(observed,payload))hold();await persist(first.name,'present',null);}
   }else{
    if(observed&&!v1RecordMatches(observed,payload))hold();
    if(!observed){await refresh();if(current!.effects![0].status!=='prepared')hold();await submit(first.name);const after=await observe();policyHash=after.policyHash;observed=after.source;if(!observed||!v1RecordMatches(observed,payload))hold();}
    await persist(first.name,'present',null);
   }
   {const decision=await observe();policyHash=decision.policyHash;observed=decision.source;}
   if(!policyHash){
    if(retained.get(first.name)?.outcome!=='present')hold();if(!observed||!v1RecordMatches(observed,payload))hold();
    await persist(native.name,'not-needed',null);
   }
  }else{
   const historic=retained.get(first.name);
   if(historic){if(!policyHash||policyHash!==historic.evidence.policyHash)hold();}
   else{
    let outcome:Outcome='already-excluded';await refresh();
    if(!policyHash){if(current!.effects![0].status!=='prepared')hold();await submit(first.name);const after=await observe();policyHash=after.policyHash;observed=after.source;if(!policyHash)hold();}
    await refresh();
    if(current!.effects![0].status!=='prepared'){
     const ownRecord=await buildV1ExclusionRecord({controlType:control,targetDataType:target,targetRecordId:recordId,actionId:payload.action_id as string,requestedAt:payload.start_time as string,reason:payload.reason as string,evidence:payload.evidence as string[]});await checked();const own=await sha256Hex(stable(ownRecord));await checked();if(own!==policyHash)hold();outcome='present';
    }
    await persist(first.name,outcome,policyHash);
   }
  }
  if(policyHash){
   const historic=retained.get(native.name);if(historic&&(historic.outcome!=='absent'||historic.evidence.policyHash!==policyHash))hold();
   const before=await observe();observed=before.source;if(before.policyHash!==policyHash)hold();
   if(observed){await refresh();if(current!.effects![1].status!=='prepared')hold();await submit(native.name);const after=await observe();observed=after.source;if(observed||after.policyHash!==policyHash)hold();}
   await persist(native.name,'absent',policyHash);
  }
  // Successful reuse is also a guarded current-owner CAS; no generic stored
  // proof may bypass original local tuple predicates at the final boundary.
  const final=await observe(),finalSource=final.source,finalPolicy=final.policyHash;if(finalPolicy!==policyHash)hold();
  if(policyHash?!!finalSource:!finalSource||!v1RecordMatches(finalSource,payload))hold();
  for(const plan of plans){const proof=retained.get(plan.name);if(!proof)hold();await journal.verifyGuardedEffect(http.scope,original,plan.name,authority,proof,deadline,signal);await checked();}
  remaining();const result:Readonly<V1ActionObservation>=freeze({...base,status:'observed' as const,ownerHeld:true,outcome:policyHash?'excluded' as const:'present' as const,original:{intent:original,targetDataType:target,targetRecordId:recordId,controlDataType:control,bodyText:primary.bodyText,bodyHash:first.bodyHash,dispatchAuthority:authority},phaseProofs:plans.map(p=>retained.get(p.name)!),currentEvidence:{...initial,policyHash}});observations.set(result,{journal,deadline});return result;
 };
 try{
  if(!Number.isFinite(timeoutMs)||timeoutMs<=0||timeoutMs>30000||signal.aborted)return unknown();
  const timeout=new Promise<Readonly<V1ActionObservation>>(resolve=>{timer=setTimeout(()=>{cancel();resolve(unknown());},Math.ceil(remaining()));signal.addEventListener('abort',()=>resolve(unknown()),{once:true});});
  return await Promise.race([operation(),timeout]);
 }catch{return unknown();}finally{cancel();clearTimeout(timer);external?.removeEventListener('abort',cancel);}
}

/** Historical receipts describe local commit plus provider evidence as of its
 * observation. UNKNOWN may include a committed transaction whose completion
 * was not observed; retry always inspects retained history first. */
export type V1ActionSettlement =
 | Readonly<{status:'settled'|'already-settled';receipt:V1MutationIntent}>
 | Readonly<{status:'unknown';localCommit:'unknown'}>;
export async function settleV1Action(options:Options):Promise<V1ActionSettlement> {
 const started=performance.now(),timeoutMs=options.timeoutMs??30000,deadline=started+timeoutMs;
 const journal=options.journal,http=options.http,external=options.signal,controller=new AbortController();
 const unknown=():V1ActionSettlement=>Object.freeze({status:'unknown',localCommit:'unknown'});
 let timer:ReturnType<typeof setTimeout>|undefined;
 const cancel=()=>controller.abort();external?.addEventListener('abort',cancel,{once:true});
 if(external?.aborted)cancel();
 try{
  if(!Number.isFinite(started)||!Number.isFinite(timeoutMs)||timeoutMs<=0||timeoutMs>30000)return unknown();
  timer=setTimeout(cancel,Math.ceil(timeoutMs));
  const intent=freeze(snapshotV1JSON(options.intent,2*1024*1024) as unknown as V1MutationIntent);
  const remaining=()=>{const left=deadline-performance.now();if(controller.signal.aborted||left<=0)hold();return left;};
  remaining();http.assertJournal(journal);
  const history=await journal.readSettlement(http.scope,intent,deadline,controller.signal);
  if(history)return Object.freeze({status:'already-settled',receipt:history});
  if(intent.operation==='deliver'&&!effectPlans(intent)[0].bodyRef)return unknown();
  const fresh=await http.captureLocalGuard(deadline,controller.signal);remaining();
  const observed=await reconcileV1Action({http,journal,intent,timeoutMs:remaining(),signal:controller.signal});
  remaining();if(observed.status!=='observed')return unknown();await fresh();remaining();
  // Await the actual transaction outcome: never race local commit against a
  // timeout promise, and never recheck cancellation after confirmed completion.
  return await journal.settleGuardedMutation(http.scope,intent,observed,deadline,controller.signal);
 }catch{return unknown();}finally{clearTimeout(timer);external?.removeEventListener('abort',cancel);controller.abort();}
}
