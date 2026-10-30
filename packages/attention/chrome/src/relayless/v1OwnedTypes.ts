// Opt-in owned CUSTOM type inventory. The aggregate data-service catalog may
// omit failed input-service lookups, so it cannot prove absence or uniqueness.
import type { V1Http } from "./v1Http";
const ID=/^(Event|Metric)\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
export interface V1OwnedType {
  readonly id:string;
  readonly name:string;
  readonly api_version:"v1";
  readonly recordable:true;
  readonly deprecated:boolean;
  readonly record_spec:Readonly<Record<string,unknown>>;
  readonly fulcra_userid:string;
  readonly [key:string]:unknown;
}
function freeze<T>(value:T):T {
  if(value&&typeof value==="object") {
    for(const child of Object.values(value))freeze(child);
    Object.freeze(value);
  }
  return value;
}
/** API principal/gate proof and complete-response checks belong to V1Http.
 * This function validates the WHOLE direct response, never a filtered subset.
 * The endpoint is authenticated-caller-owned and normally omits an owner;
 * contradictory supplied ownership fails rather than silently being stamped.
 */
export async function readV1OwnedTypes(http:V1Http,timeoutMs?:number):Promise<readonly V1OwnedType[]> {
  const scope=http.scope;
  const rows=await http.json("/input/v1/data_type?include_deprecated=true",{timeoutMs});
  if(!Array.isArray(rows)||rows.length>4096)throw new Error("Attention v1 owned type list is unreadable or exceeds its limit");
  const result:V1OwnedType[]=[],seen=new Set<string>();
  for(const row of rows) {
    if(!row||typeof row!=="object"||Array.isArray(row)||typeof row.id!=="string"||!ID.test(row.id)
      ||seen.has(row.id)||row.api_version!=="v1"||row.recordable!==true||typeof row.deprecated!=="boolean"
      ||typeof row.name!=="string"||!row.name.trim()||!row.record_spec||typeof row.record_spec!=="object"
      ||Array.isArray(row.record_spec)||row.record_spec.type!==row.id.split("/")[0].toLowerCase()
      ||(row.fulcra_userid!==undefined&&row.fulcra_userid!==scope.accountId)
      ||(row.userid!==undefined&&row.userid!==scope.accountId))throw new Error("Attention v1 owned type identity or metadata is unreadable");
    seen.add(row.id);
    result.push(freeze({...row,fulcra_userid:scope.accountId!}) as V1OwnedType);
  }
  return Object.freeze(result);
}
