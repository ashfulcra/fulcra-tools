// Pure canonical replay-policy spec/schema proof. No owned binding, live policy,
// creation, deletion, upload or delivery authority follows from these helpers.
import contract from "../../../../fulcra-common/fulcra_common/record_exclusions_contract.json";
import {pyJsonStringify} from "./pyjson";
import {sha256Hex} from "./sha256";
import {proveV1EventSchema,proveV1EventInputSchema} from "./v1EventSchema";
const contractText=JSON.stringify(contract);
const schema=pyJsonStringify({type:"object",properties:contract.fields,required:contract.required_fields});
const body={name:contract.name,description:contract.description,record_spec:{type:"event",schema},metadata:{fulcra_tools:{key:contract.key,schema_version:1}}};
const bodyText=pyJsonStringify(body);
function freeze<T>(value:T):T {
 if(value&&typeof value==="object"){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;
}
export function exclusionV1Contract():typeof contract{return JSON.parse(contractText);}
export function exclusionV1CreateBody():Readonly<typeof body>{return freeze(JSON.parse(bodyText));}
export async function exclusionV1Fingerprint():Promise<string>{return sha256Hex(bodyText);}
export const proveExclusionV1Schema=(text:string)=>proveV1EventSchema(text,"exclusions");
export const proveExclusionV1InputSchema=(text:string)=>proveV1EventInputSchema(text,"exclusions");
