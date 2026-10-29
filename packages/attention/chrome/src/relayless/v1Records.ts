// Read-only identity proof, not coverage, replay policy, schema/ownership or
// delivery acknowledgement. No writes or queue effects occur here.
import {V1Http,V1HttpError,snapshotV1JSON} from "./v1Http";
import {parseV1Instant} from "./v1Time";
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
function refuse():never {throw new V1HttpError("record_readback");}
function frozen<T>(value:T):T {
 if(value&&typeof value==="object"){for(const child of Object.values(value))frozen(child);Object.freeze(value);}return value;
}
/** At most 32 IDs per operation; a caller coordinates multiple bounded reads.
 * A successful empty set is only absence of these exact IDs at this read. */
export async function readV1ExactRecords(http:V1Http,dataType:string,identifiers:readonly string[],timeoutMs?:number):Promise<Readonly<Record<string,Readonly<Record<string,unknown>>>>> {
 const ids=snapshotV1JSON(identifiers,2048) as string[];
 if(!Array.isArray(ids)||!ids.length||ids.length>32||ids.some(id=>typeof id!=="string"||!UUID.test(id)))refuse();
 const rows=await http.records(dataType,ids,timeoutMs);
 if(!Array.isArray(rows)||rows.length>new Set(ids).size)refuse();
 const found:Record<string,Record<string,unknown>>=Object.create(null);
 for(const row of rows) {
  if(!row||typeof row!=="object"||Array.isArray(row)||typeof row.id!=="string"||!ids.includes(row.id)||Object.hasOwn(found,row.id))refuse();
  if(["userid","fulcra_userid"].some(key=>Object.hasOwn(row,key)&&row[key]!==http.scope.accountId))refuse();
  found[row.id]=row;
 }
 return frozen(found);
}
function equal(left:unknown,right:unknown):boolean {
 if(left===right)return true;
 if(!left||!right||typeof left!=="object"||typeof right!=="object"||Array.isArray(left)!==Array.isArray(right))return false;
 const a=Object.keys(left),b=Object.keys(right);if(a.length!==b.length||!a.every(key=>Object.hasOwn(right,key)))return false;
 return a.every(key=>equal((left as Record<string,unknown>)[key],(right as Record<string,unknown>)[key]));
}
/** Require every submitted field; allow only root server-added fields. */
export function v1RecordMatches(actual:unknown,expected:unknown):boolean {
 try {
  const left=snapshotV1JSON(actual,4*1024*1024) as Record<string,unknown>,right=snapshotV1JSON(expected,4*1024*1024) as Record<string,unknown>;
  if(!left||!right||typeof left!=="object"||typeof right!=="object"||Array.isArray(left)||Array.isArray(right))return false;
  for(const [key,value]of Object.entries(right)) {
   if(!Object.hasOwn(left,key))return false;
   if(key==="start_time"||key==="end_time") {
    if(typeof left[key]!=="string"||typeof value!=="string"||parseV1Instant(left[key])!==parseV1Instant(value))return false;
   }else if(!equal(left[key],value))return false;
  }
  return true;
 }catch{return false;}
}
