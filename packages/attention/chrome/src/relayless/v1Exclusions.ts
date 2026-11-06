// Pure permanent replay-exclusion records. No owned binding, current policy,
// deletion, delivery or durable-action authority follows from encoding a row.
import {snapshotV1JSON} from "./v1Http";
import {recordIdFor} from "./v1Wire";
import {parseV1Instant,formatV1Instant} from "./v1Time";
import {exclusionV1Contract} from "./v1ExclusionSpec";
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const TYPE=/^(Event|Metric)\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const KEYS=["controlType","targetDataType","targetRecordId","actionId","requestedAt","reason","evidence"];
const reasons=exclusionV1Contract().fields.reason.enum;
export interface V1ExclusionInput {
 controlType:string;targetDataType:string;targetRecordId:string;actionId:string;requestedAt:string;reason:string;evidence:readonly string[];
}
function refuse():never{throw new Error("Collect v1 exclusion record is unsupported or mismatched");}
function primary(control:string,target:string,identifier:string):string {
 if(typeof control!=="string"||!control.startsWith("Event/")||!TYPE.test(control)
  ||typeof target!=="string"||!TYPE.test(target)||control===target||typeof identifier!=="string"||!UUID.test(identifier))refuse();
 return `collect:v1:excluded:${target}:${identifier}`;
}
export async function exclusionIdFor(control:string,target:string,identifier:string):Promise<string> {
 return recordIdFor(control,primary(control,target,identifier));
}
/** Match Python's stable target identity and flat Event shape. The action,
 * time and evidence must already have been captured durably before a caller
 * may POST anything; this pure helper does not establish that audit trail. */
export async function buildV1ExclusionRecord(input:V1ExclusionInput) {
 const value=snapshotV1JSON(input,64*1024) as V1ExclusionInput;
 if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).length!==KEYS.length
  ||!KEYS.every(key=>Object.hasOwn(value,key)))refuse();
 const source=primary(value.controlType,value.targetDataType,value.targetRecordId);
 if(typeof value.actionId!=="string"||!UUID.test(value.actionId)||typeof value.reason!=="string"||!reasons.includes(value.reason)
  ||typeof value.requestedAt!=="string"||!Array.isArray(value.evidence)||value.evidence.length>128
  ||value.evidence.some(item=>typeof item!=="string"||!item||[...item].length>1024))refuse();
 const timestamp=formatV1Instant(parseV1Instant(value.requestedAt));
 const id=await recordIdFor(value.controlType,source);
 return Object.freeze({schema_version:1,target_data_type:value.targetDataType,target_record_id:value.targetRecordId,
  action_id:value.actionId,reason:value.reason,evidence:Object.freeze([...value.evidence]),id,
  start_time:timestamp,end_time:timestamp,sources:Object.freeze([source]),tags:Object.freeze([] as string[])});
}
