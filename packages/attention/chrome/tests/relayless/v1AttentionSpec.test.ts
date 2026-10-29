// @vitest-environment node
import { describe, expect, test } from "vitest";
import { attentionV1Contract } from "../../src/relayless/v1Wire";
import { attentionV1CreateBody, attentionV1Fingerprint, proveAttentionV1Schema, proveAttentionV1InputSchema } from "../../src/relayless/v1AttentionSpec";
import { sha256Hex } from "../../src/relayless/sha256";
const contract=attentionV1Contract();
// Synthetic fixture of the verified current Event base-schema shape, without
// account IDs, live catalog names, records or private metadata.
function schema():Record<string,any>{return {type:"object",title:"RecordSchema",properties:{
 id:{title:"Id",default:null,anyOf:[{type:"string",format:"uuid"},{type:"null"}]},
 sources:{title:"Sources",default:null,anyOf:[{type:"array",items:{type:"string"}},{type:"null"}]},
 tags:{title:"Tags",default:null,anyOf:[{type:"array",items:{type:"string",format:"uuid"}},{type:"null"}]},
 start_time:{title:"Start Time",default:null,anyOf:[{type:"string",format:"date-time"},{type:"null"}]},
 end_time:{title:"End Time",default:null,anyOf:[{type:"string",format:"date-time"},{type:"null"}]},
 ...structuredClone(contract.fields)},required:[...contract.required_fields]};}
describe("Attention v1 owned specification and served schema proof",()=>{
 test("input proof retains original bytes while never evaluating known immutable base transforms",async()=>{
  const input=schema();for(const name of ["id","start_time","end_time"])input.properties[name]["x-fulcra-transform"]="synthetic: never evaluate";
  const text=JSON.stringify(input),proof=await proveAttentionV1InputSchema(text);
  expect(proof.inputSchemaText).toBe(text);expect(proof.inputSchemaHash).toBe(await sha256Hex(text));expect(proof.schema).toEqual(schema());
 });
 test.each(["note","sources","tags"])("input proof does not redact unsupported transform on %s",async name=>{
  const input=schema();input.properties[name]["x-fulcra-transform"]="synthetic";await expect(proveAttentionV1InputSchema(JSON.stringify(input))).rejects.toThrow();
 });
 test.each([null,{},"","\ud800"])("malformed base transform refuses %j",async value=>{
  const input=schema();input.properties.id["x-fulcra-transform"]=value;await expect(proveAttentionV1InputSchema(JSON.stringify(input))).rejects.toThrow();
 });
 test("create body and fingerprint are byte-compatible with the pinned Python spec",async()=>{
  const body=attentionV1CreateBody();expect(body.name).toBe("Attention");expect(body.record_spec.type).toBe("event");
  const fragment=JSON.parse(body.record_spec.schema);expect(fragment).toEqual({type:"object",properties:contract.fields,required:contract.required_fields});
  expect(body.metadata).toEqual({fulcra_tools:{key:"collect.attention.v1",schema_version:1}});
  expect(await attentionV1Fingerprint()).toBe("005994ac66e5f4548050c843c0a261891d59d358c8f67578f1cb6dd93b8ba941");
  expect(Object.isFrozen(body)&&Object.isFrozen(body.record_spec)&&Object.isFrozen(body.metadata.fulcra_tools)).toBe(true);
 });
 test("proves actual nullable base plus exact Attention fields and hashes retained text bytes",async()=>{
  const text=JSON.stringify(schema());const proof=await proveAttentionV1Schema(text);
  expect(proof.schema).toEqual(schema());expect(proof.schemaText).toBe(text);expect(proof.schemaHash).toBe(await sha256Hex(text));
  expect(Object.isFrozen(proof)&&Object.isFrozen(proof.schema)&&Object.isFrozen(proof.schema.properties.external_ids.properties)).toBe(true);
 });
 test("valid Unicode annotations and provided base-required fields stay supported",async()=>{
  const input=schema();input.description="Synthetic café 😀";input.required.push("id","sources","tags","start_time","end_time");input.additionalProperties=false;
  const proof=await proveAttentionV1Schema(JSON.stringify(input));expect(proof.schema.description).toBe(input.description);
 });
 test("schema text digest is explicit and preserves its exact formatting",async()=>{
  const a=await proveAttentionV1Schema(JSON.stringify(schema())),b=await proveAttentionV1Schema(JSON.stringify(schema(),null,2));
  expect(a.schema).toEqual(b.schema);expect(a.schemaHash).not.toBe(b.schemaHash);
 });
 test("nonnull base variants and equivalent set ordering are accepted",async()=>{
  const input=schema();for(const key of ["id","sources","tags","start_time","end_time"])input.properties[key]=input.properties[key].anyOf[0];
  input.required.reverse();input.properties.title.type.reverse();input.properties.external_ids.required.reverse();
  await expect(proveAttentionV1Schema(JSON.stringify(input))).resolves.toBeDefined();
 });
 const changes:Record<string,(value:Record<string,any>)=>void>={
  "missing field":s=>{delete s.properties.note;},
  "wrong note type":s=>{s.properties.note.type="number";},
  "missing declared required field":s=>{s.required=s.required.filter((key:string)=>key!=="note");},
  "duplicate required":s=>{s.required.push("note");},
  "unsupported required field":s=>{s.required.push("unknown");},
  "extra optional field":s=>{s.properties.unknown={type:"string"};},
  "service mismatch":s=>{s.properties.service.const="other";},
  "negative duration allowed":s=>{delete s.properties.duration_seconds.minimum;},
  "extra field restriction":s=>{s.properties.note.maxLength=1;},
  "root restriction":s=>{s.maxProperties=1;},
  "hidden allOf":s=>{s.allOf=[{properties:{service:{const:"other"}}}];},
  "root reference":s=>{s.$ref="https://example.com/schema";},
  "local reference":s=>{s.properties.note={$ref:"#/$defs/note"};s.$defs={note:{type:"string"}};},
  "unknown required type":s=>{s.required="note";},
  "wrong root":s=>{s.type="array";},
  "wrong base":s=>{s.properties.id={type:"number"};},
  "base restriction":s=>{s.properties.sources.maxItems=0;},
  "nested external restriction":s=>{s.properties.external_ids.properties.client.pattern="^one$";},
  "nested external extra":s=>{s.properties.external_ids.properties.unknown={type:"string"};},
  "nested open object":s=>{s.properties.external_ids.additionalProperties=true;},
  "nested missing required":s=>{s.properties.external_ids.required.pop();},
  "invalid annotation":s=>{s.title=123;},
  "annotation invalid Unicode":s=>{s.description="\ud800";},
  "unknown dialect":s=>{s.$schema="https://example.com/unknown";},
  "pattern additional properties":s=>{s.additionalProperties={type:"null"};},
 };
 for(const [name,change]of Object.entries(changes))test("unsupported/incompatible schema holds: "+name,async()=>{
  const input=schema();change(input);await expect(proveAttentionV1Schema(JSON.stringify(input))).rejects.toThrow();
 });
 test.each(["", "null", "[]", "{", "NaN", "x".repeat(1024*1024+1)])("invalid/oversized schema text refuses",async text=>{
  await expect(proveAttentionV1Schema(text)).rejects.toThrow();
 });
 test("mutating a returned contract cannot change the owned creation spec",async()=>{
  const altered=attentionV1Contract();altered.name="Other";altered.fields.note.type="number";
  expect(attentionV1CreateBody().name).toBe("Attention");expect(await attentionV1Fingerprint()).toBe("005994ac66e5f4548050c843c0a261891d59d358c8f67578f1cb6dd93b8ba941");
 });
});

test("duplicate schema assertions remain ambiguous for Attention too",async()=>{
 const text=JSON.stringify(schema()).replace('"minimum":0','"minimum":9,"minimum":0');
 await expect(proveAttentionV1Schema(text)).rejects.toThrow();await expect(proveAttentionV1InputSchema(text)).rejects.toThrow();
});
