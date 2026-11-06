// Network-free immutable body plans. Byte integrity is not semantic schema,
// provider truth, dispatch permission or queue settlement.
import {snapshotV1JSON,parseV1JSON} from "./v1Http";
import {decodeQueuedAttentionRow} from "./v1QueueCodec";
import type {V1OutboxRow} from "./v1Journal";
export const LEGACY_REQUEST_LIMIT=65536,PHASE_REQUEST_LIMIT=256*1024,INLINE_BODY_LIMIT=128*1024;
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/,HASH=/^[a-f0-9]{64}$/;
export interface EffectBodyReference {accountId:string;generation:string;dataType:string;recordId:string;envelopeHash:string;envelopeBytes:number;}
export interface EffectPlan {name:string;destination:string;bodyText:string|null;bodyHash:string;bodyBytes:number;bodyRef?:EffectBodyReference;}
function refuse():never {throw new Error("Attention v1 original effect body is unavailable; retain queued data");}
function object(value:unknown):value is Record<string,unknown>{return !!value&&typeof value==="object"&&!Array.isArray(value);}
function exact(value:unknown,names:string[]):value is Record<string,unknown>{return object(value)&&Object.keys(value).length===names.length&&names.every(key=>Object.hasOwn(value,key));}
function uuid(value:unknown):value is string{return typeof value==="string"&&UUID.test(value);}
function event(value:unknown):value is string{return typeof value==="string"&&value.startsWith("Event/")&&uuid(value.slice(6));}
export function phasedRequestLimit(text:string):number {
 const value=parseV1JSON(text);if(!object(value))refuse();
 if(value.version===1)return LEGACY_REQUEST_LIMIT;if(value.version===2)return PHASE_REQUEST_LIMIT;return refuse();
}
export function effectPlans(row:{requestText:string;operation:"deliver"|"exclude"}):EffectPlan[] {
 if(row.requestText.length>PHASE_REQUEST_LIMIT)refuse();
 const value=parseV1JSON(row.requestText),limit=phasedRequestLimit(row.requestText);
 if(new TextEncoder().encode(row.requestText).length>limit||!exact(value,["version","details","effects"])||!object(value.details)
  ||!Array.isArray(value.effects)||value.effects.length!==2)refuse();
 const names=row.operation==="deliver"?["deliver","repair"]:["policy","delete"],plans:EffectPlan[]=[];
 for(let i=0;i<2;i++){
  const raw=value.effects[i],referenced=value.version===2&&object(raw)&&Object.hasOwn(raw,"bodyRef"),max=value.version===1?LEGACY_REQUEST_LIMIT:INLINE_BODY_LIMIT;
  if(!exact(raw,["name","destination",referenced?"bodyRef":"bodyText","bodyHash","bodyBytes"])||raw.name!==names[i]
   ||typeof raw.destination!=="string"||(i===0?!event(raw.destination):raw.destination!=="DeletedRecord")
   ||typeof raw.bodyHash!=="string"||!HASH.test(raw.bodyHash)||!Number.isSafeInteger(raw.bodyBytes)||(raw.bodyBytes as number)<1)refuse();
  if(referenced){
   const ref=raw.bodyRef;
   if(i!==0||row.operation!=="deliver"||!exact(ref,["accountId","generation","dataType","recordId","envelopeHash","envelopeBytes"])
    ||!uuid(ref.accountId)||!uuid(ref.generation)||ref.dataType!==raw.destination||!uuid(ref.recordId)
    ||typeof ref.envelopeHash!=="string"||!HASH.test(ref.envelopeHash)||!Number.isSafeInteger(ref.envelopeBytes)
    ||(ref.envelopeBytes as number)<1||(ref.envelopeBytes as number)>4*1024*1024||(raw.bodyBytes as number)>4*1024*1024||(raw.bodyBytes as number)>(ref.envelopeBytes as number))refuse();
   plans.push({name:raw.name as string,destination:raw.destination,bodyText:null,bodyHash:raw.bodyHash,bodyBytes:raw.bodyBytes as number,bodyRef:ref as unknown as EffectBodyReference});
  }else{
   if(typeof raw.bodyText!=="string"||raw.bodyText.length>max||raw.bodyBytes!==new TextEncoder().encode(raw.bodyText).length||(raw.bodyBytes as number)>max)refuse();
   const lines=raw.bodyText.split("\n");if(!lines.length||lines.length>32)refuse();
   for(const line of lines){const record=snapshotV1JSON(parseV1JSON(line),max);if(!object(record)||JSON.stringify(record)!==line)refuse();}
   plans.push({name:raw.name as string,destination:raw.destination,bodyText:raw.bodyText,bodyHash:raw.bodyHash,bodyBytes:raw.bodyBytes as number});
  }
 }
 return plans;
}
export function referenceFor(row:V1OutboxRow):EffectBodyReference {
 return {accountId:row.accountId,generation:row.generation,dataType:row.dataType,recordId:row.recordId,envelopeHash:row.hash,envelopeBytes:row.bytes};
}
export async function referencedBody(owner:{accountId:string;generation:string},plan:EffectPlan,row:V1OutboxRow,checkEvidence?:()=>void):Promise<string> {
 checkEvidence?.();
 const ref=plan.bodyRef;
 if(!row||!ref||ref.accountId!==owner.accountId||ref.generation!==owner.generation||JSON.stringify(referenceFor(row))!==JSON.stringify(ref)
  ||row.dataType!==plan.destination)refuse();
 // Use the SAME pure scope/schema/source/record/hash reproduction as V1Outbox.
 // Outer metadata alone cannot reauthorize an old hashed account/generation.
 const entry=await decodeQueuedAttentionRow(row,checkEvidence);checkEvidence?.();
 if(entry.payloadHash!==plan.bodyHash||new TextEncoder().encode(entry.payloadText).length!==plan.bodyBytes)refuse();
 return entry.payloadText;
}
