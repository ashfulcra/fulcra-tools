// Opt-in immutable body/type/tag preflight. Replay policy, exact readback and
// logical/queue settlement belong to the subsequent actor, never this receipt leg.
import type {V1Http} from "./v1Http";
import {parseV1JSON,snapshotV1JSON} from "./v1Http";
import type {V1Journal,V1MutationIntent,V1DispatchAuthority} from "./v1Journal";
import {effectPlans} from "./v1EffectBodies";
import {buildV1ExclusionRecord} from "./v1Exclusions";
import {attentionV1Contract} from "./v1Wire";
import {exclusionV1Contract} from "./v1ExclusionSpec";
import {v1RecordMatches} from "./v1Records";
import {withV1DecisionFailure,requireV1DecisionCapability,withV1DecisionAuthority,type V1DecisionCapability} from "./v1DecisionAuthority";
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const TYPE=/^Event\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
function refuse():never{throw new Error("Attention v1 typed effect cannot be verified; retain original work");}
function object(value:unknown):value is Record<string,unknown>{return !!value&&typeof value==="object"&&!Array.isArray(value);}
function exact(value:unknown,keys:string[]):value is Record<string,unknown>{return object(value)&&Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));}
/** Same URL-namespace UUID5 identity as the Python native v1 deletion writer. */
export async function buildV1NativeDeletion(target:string,recordId:string,actionId:string):Promise<Readonly<{id:string;record_id:string;data_type:"Event"}>> {
 if(typeof target!=="string"||!TYPE.test(target)||typeof recordId!=="string"||!UUID.test(recordId)||typeof actionId!=="string"||!UUID.test(actionId))refuse();
 const namespace=new Uint8Array([0x6b,0xa7,0xb8,0x11,0x9d,0xad,0x11,0xd1,0x80,0xb4,0x00,0xc0,0x4f,0xd4,0x30,0xc8]);
 const name=new TextEncoder().encode(`fulcra-tools:v1:delete:${actionId}:${target}:${recordId}`),bytes=new Uint8Array(namespace.length+name.length);bytes.set(namespace);bytes.set(name,namespace.length);
 const digest=new Uint8Array(await crypto.subtle.digest("SHA-1",bytes.buffer)).slice(0,16);digest[6]=(digest[6]&15)|80;digest[8]=(digest[8]&63)|128;
 const h=Array.from(digest,byte=>byte.toString(16).padStart(2,"0")).join("");return Object.freeze({id:`${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`,record_id:recordId,data_type:"Event"});
}
const annotations=new Set(["title","description","$comment","default","examples"]);
function shape(actual:unknown,expected:Record<string,unknown>):boolean {
 if(!object(actual))return false;
 for(const [key,value]of Object.entries(actual)){
  if(Object.hasOwn(expected,key)){if(JSON.stringify(value)!==JSON.stringify(expected[key]))return false;}
  else if(!annotations.has(key)||(key!=="default"&&(key==="examples"?!Array.isArray(value):typeof value!=="string")))return false;
 }
 return Object.keys(expected).every(key=>Object.hasOwn(actual,key));
}
/** A narrow native schema boundary. Unknown assertions and required fields
 * hold; no stripping/transforms/general schema evaluation or remote references. */
export function proveV1NativeDeletionSchema(input:unknown):void {
 const schema=snapshotV1JSON(input,1024*1024);if(!object(schema)||schema.type!=="object"||!object(schema.properties))refuse();
 const fields=schema.properties,base:Record<string,Record<string,unknown>>={id:{type:"string",format:"uuid"},record_id:{type:"string",format:"uuid"},data_type:{type:"string"},sources:{type:"array",items:{type:"string"}},tags:{type:"array",items:{type:"string",format:"uuid"}},start_time:{type:"string",format:"date-time"},end_time:{type:"string",format:"date-time"}};
 if(!["id","record_id","data_type"].every(key=>Object.hasOwn(fields,key))||Object.keys(fields).some(key=>!Object.hasOwn(base,key)))refuse();
 for(const [key,value]of Object.entries(fields))if(!shape(value,base[key])&&!shape(value,{anyOf:[base[key],{type:"null"}]}))refuse();
 if(!Array.isArray(schema.required)||new Set(schema.required).size!==schema.required.length||schema.required.some(key=>!["id","record_id","data_type"].includes(key as string)))refuse();
 for(const [key,value]of Object.entries(schema)){
  if(["type","properties","required"].includes(key))continue;
  if(key==="additionalProperties"){if(typeof value!=="boolean")refuse();continue;}
  if(key==="$schema"){if(!["https://json-schema.org/draft/2020-12/schema","http://json-schema.org/draft-07/schema#"].includes(value as string))refuse();continue;}
  if(!annotations.has(key)||(key!=="default"&&(key==="examples"?!Array.isArray(value):typeof value!=="string")))refuse();
 }
}
export async function prepareV1TypedEffect(http:V1Http,journal:V1Journal,intent:V1MutationIntent,name:string,authority:V1DispatchAuthority,deadline:number,signal:AbortSignal,proof?:V1DecisionCapability):Promise<Readonly<{destination:string;bodyText:string}>> {
 return withV1DecisionFailure(proof,async()=>{
 const remaining=()=>{const now=performance.now();if(signal.aborted||!Number.isFinite(now)||now>=deadline)refuse();return deadline-now;};
 const fresh=await http.captureLocalGuard(deadline,signal);
 const checked=async()=>{remaining();await fresh();if(proof)await requireV1DecisionCapability(proof,http,journal,deadline,signal,intent,authority);remaining();};
 remaining();if(intent.version!==2||intent.generation!==http.scope.generation||intent.accountId!==http.scope.accountId||!http.scope.enabled)refuse();
 const plans=effectPlans(intent),body=await journal.readEffect(http.scope,intent,name,deadline,signal);await checked();
 const first=plans[0];let target:string,recordId:string,tagIds:string[]=[];
 if(intent.operation==="deliver"){
  if(!first.bodyRef)refuse();target=first.bodyRef.dataType;recordId=first.bodyRef.recordId;
  const source=await journal.readEffect(http.scope,intent,"deliver",deadline,signal);await checked();const record=parseV1JSON(source.bodyText);
  if(!object(record)||!Array.isArray(record.tags)||record.tags.some(tag=>typeof tag!=="string"||!UUID.test(tag)))refuse();tagIds=record.tags as string[];
 }else{
  const policyBody=await journal.readEffect(http.scope,intent,"policy",deadline,signal);await checked();const policy=parseV1JSON(policyBody.bodyText);
  if(!object(policy)||policy.action_id!==intent.actionId||typeof policy.target_data_type!=="string"||!TYPE.test(policy.target_data_type)||typeof policy.target_record_id!=="string"||!UUID.test(policy.target_record_id))refuse();
  target=policy.target_data_type;recordId=policy.target_record_id;
  const expected=await buildV1ExclusionRecord({controlType:first.destination,targetDataType:target,targetRecordId:recordId,actionId:intent.actionId,requestedAt:policy.start_time as string,reason:policy.reason as string,evidence:policy.evidence as string[]});await checked();
  if(!v1RecordMatches(policy,expected)||Object.keys(policy).length!==Object.keys(expected).length)refuse();
 }
 if(!object(authority)||!Array.isArray(authority.creations)||authority.creations.length!==2||!Array.isArray(authority.tags)||authority.tags.length>128)refuse();
 const a=authority.creations.find(row=>row.specKey===attentionV1Contract().key),c=authority.creations.find(row=>row.specKey===exclusionV1Contract().key);
 if(!a||!c||a.destination!==target||!c.destination||c.destination===target||intent.operation==="exclude"&&first.destination!==c.destination)refuse();
 if(tagIds.some(id=>authority.tags.filter(row=>row.destination===id).length!==1))refuse();
 if(!proof){
  const prepared=await withV1DecisionAuthority({http,journal,intent,authority,target,control:c.destination,deadline,signal},(reads,capability)=>prepareV1TypedEffect(reads,journal,intent,name,authority,deadline,signal,capability));await checked();return prepared;
 }
 const proved=await requireV1DecisionCapability(proof,http,journal,deadline,signal,intent,authority);await checked();
 if(proved.target!==target||proved.control!==c.destination||proved.recordId!==recordId)refuse();
 // Validate the complete immutable native plan before the PRIMARY write too;
 // otherwise a valid source/policy could strand an unrepairable fixed action.
 const native=await journal.readEffect(http.scope,intent,plans[1].name,deadline,signal);await checked();
 const expectedNative=await buildV1NativeDeletion(target,recordId,intent.actionId);await checked();const nativeRecord=parseV1JSON(native.bodyText);
 if(!exact(nativeRecord,["id","record_id","data_type"])||!v1RecordMatches(nativeRecord,expectedNative))refuse();
 await requireV1DecisionCapability(proof,http,journal,deadline,signal,intent,authority);await checked();
 remaining();return Object.freeze({destination:body.destination,bodyText:body.bodyText});
 });
}
