// Attention's immutable specification and wrappers around the closed Event family.
import {attentionV1Contract} from "./v1Wire";
import {pyJsonStringify} from "./pyjson";
import {sha256Hex} from "./sha256";
import {proveV1EventSchema,proveV1EventInputSchema} from "./v1EventSchema";
export type {V1EventSchemaProof as AttentionV1SchemaProof,V1EventInputSchemaProof as AttentionV1InputSchemaProof} from "./v1EventSchema";
const contract=attentionV1Contract();
const fragment=pyJsonStringify({type:"object",properties:contract.fields,required:contract.required_fields});
const body={name:contract.name,description:contract.description,record_spec:{type:"event",schema:fragment},
 metadata:{fulcra_tools:{key:contract.key,schema_version:1}}};
const bodyText=pyJsonStringify(body);
function freeze<T>(value:T):T {
 if(value&&typeof value==="object"){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;
}
export function attentionV1CreateBody():Readonly<typeof body>{return freeze(JSON.parse(bodyText));}
export async function attentionV1Fingerprint():Promise<string>{return sha256Hex(bodyText);}
export const proveAttentionV1Schema=(text:string)=>proveV1EventSchema(text,"attention");
export const proveAttentionV1InputSchema=(text:string,checkEvidence?:()=>void)=>proveV1EventInputSchema(text,"attention",checkEvidence);
