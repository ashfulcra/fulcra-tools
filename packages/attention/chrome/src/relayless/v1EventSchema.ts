// Opt-in pure schema/spec proof. This is a closed, verified schema family,
// not a general JSON Schema interpreter. Unknown assertions must remain held.
import attention from "../../../fulcra_attention/v1_contract.json";
import exclusions from "../../../../fulcra-common/fulcra_common/record_exclusions_contract.json";
import { sha256Hex } from "./sha256";
import {parseV1JSON} from "./v1Http";
const contracts=freeze({attention,exclusions});
export type V1EventFamily="attention"|"exclusions";
function contractFor(family:V1EventFamily){
 if(family!=="attention"&&family!=="exclusions")return refuse();return contracts[family];
}
const annotations=new Set(["title","description","$comment","default","examples"]);
const baseFields:Record<string,Record<string,unknown>>={
 id:{type:"string",format:"uuid"},sources:{type:"array",items:{type:"string"}},
 tags:{type:"array",items:{type:"string",format:"uuid"}},
 start_time:{type:"string",format:"date-time"},end_time:{type:"string",format:"date-time"},
};
function refuse():never{throw new Error("Collect v1 served schema is unsupported or incompatible");}
function object(value:unknown):value is Record<string,unknown>{return value!==null&&typeof value==="object"&&!Array.isArray(value);}
function unicode(value:string):boolean {
 for(const point of value){const code=point.codePointAt(0)!;if(code>=0xd800&&code<=0xdfff)return false;}return true;
}
function freeze<T>(value:T):T {
 if(value&&typeof value==="object"){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;
}
function validJSON(value:unknown):void {
 const pending=[{value,depth:0}];let count=0;
 while(pending.length){const row=pending.pop()!,item=row.value;
  if(++count>100000||row.depth>64)refuse();
  if(typeof item==="string"){if(!unicode(item))refuse();}
  else if(typeof item==="number"){if(!Number.isFinite(item)||(Number.isInteger(item)&&!Number.isSafeInteger(item)))refuse();}
  else if(item&&typeof item==="object"){
   if(Object.keys(item).some(key=>!unicode(key)))refuse();
   for(const child of Object.values(item))pending.push({value:child,depth:row.depth+1});
  }
 }
}
function annotation(key:string,value:unknown):boolean {
 if(key==="default")return true;
 if(key==="examples")return Array.isArray(value);
 return typeof value==="string";
}
function sameSet(actual:unknown,expected:unknown[]):boolean {
 return Array.isArray(actual)&&actual.length===expected.length&&new Set(actual).size===actual.length
  &&actual.every(value=>typeof value==="string"&&expected.includes(value));
}
function properties(actual:unknown,expected:Record<string,unknown>):boolean {
 return object(actual)&&Object.keys(actual).length===Object.keys(expected).length
  &&Object.keys(expected).every(key=>Object.hasOwn(actual,key)&&sameSchema(actual[key],expected[key]));
}
function sameSchema(actual:unknown,expected:unknown):boolean {
 if(!object(expected))return actual===expected;
 if(!object(actual))return false;
 for(const [key,value]of Object.entries(actual)) {
  if(!Object.hasOwn(expected,key)&&(!annotations.has(key)||!annotation(key,value)))return false;
 }
 for(const [key,value]of Object.entries(expected)) {
  if(!Object.hasOwn(actual,key))return false;
  if(key==="properties"){if(!properties(actual[key],value as Record<string,unknown>))return false;}
  else if(key==="items"){if(!sameSchema(actual[key],value))return false;}
  else if(key==="required"||key==="enum"||(key==="type"&&Array.isArray(value))){if(!sameSet(actual[key],value as unknown[]))return false;}
  else if(key==="anyOf") {
   if(!Array.isArray(actual[key])||actual[key].length!==(value as unknown[]).length)return false;
   const used=new Set<number>();
   for(const branch of actual[key]){const match=(value as unknown[]).findIndex((expected,index)=>!used.has(index)&&sameSchema(branch,expected));if(match<0)return false;used.add(match);}
  }else if(actual[key]!==value)return false;
 }
 return true;
}
export interface V1EventSchemaProof {
 readonly schema:Readonly<{
  type:"object";
  properties:Readonly<Record<string,Readonly<Record<string,unknown>>>>;
  required:readonly string[];
  [key:string]:unknown;
 }>;
 // Exact serialized snapshot provided by the caller, not raw HTTP wire bytes.
 readonly schemaText:string;
 readonly schemaHash:string;
}
function parseSchemaText(schemaText:string):unknown {
 if(typeof schemaText!=="string"||schemaText.length>1024*1024
   ||new TextEncoder().encode(schemaText).length>1024*1024)refuse();
 let schema:unknown;try{schema=parseV1JSON(schemaText);}catch{return refuse();}
 validJSON(schema);
 return schema;
}
export async function proveV1EventSchema(schemaText:string,family:V1EventFamily):Promise<Readonly<V1EventSchemaProof>> {
 // Parse and freeze before the first await. No caller-owned mutable object is
 // retained. The digest is portable SHA256 of this exact UTF8 text, not a
 // claim of general Python/Swift JSON-number serialization equivalence.
 const contract=contractFor(family),schema=parseSchemaText(schemaText);
 if(!object(schema)||schema.type!=="object"||!object(schema.properties))refuse();
 const fields=schema.properties;
 const allowed=new Set(["type","properties","required","additionalProperties","$schema",...annotations]);
 if(Object.entries(schema).some(([key,value])=>!allowed.has(key)||(annotations.has(key)&&!annotation(key,value))))refuse();
 if(schema.$schema!==undefined&&!['https://json-schema.org/draft/2020-12/schema','http://json-schema.org/draft-07/schema#','https://json-schema.org/draft-07/schema#'].includes(schema.$schema as string))refuse();
 if(schema.additionalProperties!==undefined&&typeof schema.additionalProperties!=="boolean")refuse();
 const expectedKeys=[...Object.keys(baseFields),...Object.keys(contract.fields)];
 if(Object.keys(fields).length!==expectedKeys.length||!expectedKeys.every(key=>Object.hasOwn(fields,key)))refuse();
 for(const [key,value]of Object.entries(baseFields)) {
  const actual=fields[key];
  if(!sameSchema(actual,value)&&!sameSchema(actual,{anyOf:[value,{type:"null"}]}))refuse();
 }
 for(const [key,value]of Object.entries(contract.fields))if(!sameSchema(fields[key],value))refuse();
 const required=schema.required;
 if(!Array.isArray(required)||new Set(required).size!==required.length
  ||required.some(key=>typeof key!=="string"||!expectedKeys.includes(key))
  ||!contract.required_fields.every(key=>required.includes(key)))refuse();
 const captured=freeze(schema) as V1EventSchemaProof["schema"];
 return Object.freeze({schema:captured,schemaText,schemaHash:await sha256Hex(schemaText)});
}

export interface V1EventInputSchemaProof extends V1EventSchemaProof {
 readonly inputSchemaText:string;
 readonly inputSchemaHash:string;
}
/** The authenticated input endpoint exposes immutable base-field transforms
 * redacted by the served-schema endpoint. Never evaluate them. Only those
 * three known base-property annotations may be removed for the closed check;
 * custom transforms or other assertions remain unsupported. Retain/digest
 * the UNSANITIZED input text too, and verify it is unchanged before binding.
 */
export async function proveV1EventInputSchema(inputSchemaText:string,family:V1EventFamily,checkEvidence?:()=>void):Promise<Readonly<V1EventInputSchemaProof>> {
 checkEvidence?.();
 contractFor(family);
 const schema=parseSchemaText(inputSchemaText);
 if(!object(schema)||!object(schema.properties))refuse();
 for(const name of ["id","start_time","end_time"]) {
  const field=schema.properties[name];
  if(!object(field))refuse();
  if(Object.hasOwn(field,"x-fulcra-transform")) {
   const transform=field["x-fulcra-transform"];
   if(typeof transform!=="string"||!transform.trim())refuse();
   delete field["x-fulcra-transform"];
  }
 }
 checkEvidence?.();const proof=await proveV1EventSchema(JSON.stringify(schema),family);checkEvidence?.();
 checkEvidence?.();const inputSchemaHash=await sha256Hex(inputSchemaText);checkEvidence?.();
 return Object.freeze({...proof,inputSchemaText,inputSchemaHash});
}
