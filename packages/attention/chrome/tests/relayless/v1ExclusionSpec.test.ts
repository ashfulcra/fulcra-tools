// @vitest-environment node
import {describe,expect,test} from "vitest";
import {exclusionV1Contract,exclusionV1CreateBody,exclusionV1Fingerprint,proveExclusionV1Schema,proveExclusionV1InputSchema} from "../../src/relayless/v1ExclusionSpec";
import {proveAttentionV1Schema} from "../../src/relayless/v1AttentionSpec";
function schema(){const c=exclusionV1Contract();return {type:"object",properties:{id:{type:"string",format:"uuid"},
 sources:{type:"array",items:{type:"string"}},tags:{type:"array",items:{type:"string",format:"uuid"}},start_time:{type:"string",format:"date-time"},end_time:{type:"string",format:"date-time"},...c.fields},required:c.required_fields};}
describe("shared permanent replay exclusion specification",()=>{
 test("creation identity and required order match existing Python",async()=>{
  expect(await exclusionV1Fingerprint()).toBe("92f7c8ea19c922fcafb1e9274e90ef9ff5974bc3e8644973fef9bd1bc5029af0");
  const body=exclusionV1CreateBody();expect(Object.isFrozen(body.record_spec)).toBe(true);
  expect(JSON.parse(body.record_spec.schema).required).toEqual(["schema_version","target_data_type","target_record_id","action_id","reason","evidence"]);
 });
 test("known control schema has a frozen exact-text proof",async()=>{
  const text=JSON.stringify(schema()),proof=await proveExclusionV1Schema(text);expect(proof.schemaText).toBe(text);expect(Object.isFrozen(proof.schema.properties)).toBe(true);
  await expect(proveAttentionV1Schema(text)).rejects.toThrow();
 });
 test("reason enum and root requirement order do not change semantics",async()=>{
  const value=schema();value.properties.reason.enum.reverse();value.required.reverse();await proveExclusionV1Schema(JSON.stringify(value));
 });
 test.each(["extraReason","missingReason","boolVersion","wrongTarget","extraField","missingRequired","ref","customTransform"])("incompatible control %s refuses",async kind=>{
  const value=schema() as any;if(kind==="extraReason")value.properties.reason.enum.push("restore");if(kind==="missingReason")value.properties.reason.enum.pop();
  if(kind==="boolVersion")value.properties.schema_version.const=true;if(kind==="wrongTarget")value.properties.target_data_type.pattern=".*";
  if(kind==="extraField")value.properties.extra={type:"string"};if(kind==="missingRequired")value.required.pop();if(kind==="ref")value.$ref="https://example.com/schema";
  if(kind==="customTransform")value.properties.reason["x-fulcra-transform"]="synthetic";await expect(proveExclusionV1Schema(JSON.stringify(value))).rejects.toThrow();
 });
 test("duplicate schema assertion keys cannot be silently discarded",async()=>{
  const text=JSON.stringify(schema()).replace('"const":1','"const":2,"const":1');
  await expect(proveExclusionV1Schema(text)).rejects.toThrow();await expect(proveExclusionV1InputSchema(text)).rejects.toThrow();
 });
 test("only known immutable base transforms may be redacted for input proof",async()=>{
  const value=schema() as any;value.properties.id["x-fulcra-transform"]="synthetic";const text=JSON.stringify(value),proof=await proveExclusionV1InputSchema(text);
  expect(proof.inputSchemaText).toBe(text);expect(proof.inputSchemaHash).not.toBe(proof.schemaHash);expect(proof.schema.properties.id["x-fulcra-transform"]).toBeUndefined();
  value.properties.reason["x-fulcra-transform"]="synthetic";await expect(proveExclusionV1InputSchema(JSON.stringify(value))).rejects.toThrow();
 });
 test("caller copies cannot mutate the canonical future spec",async()=>{const copy=exclusionV1Contract();copy.fields.reason.enum.push("restore");expect(exclusionV1Contract().fields.reason.enum).not.toContain("restore");expect(await exclusionV1Fingerprint()).toBe("92f7c8ea19c922fcafb1e9274e90ef9ff5974bc3e8644973fef9bd1bc5029af0");});
});
