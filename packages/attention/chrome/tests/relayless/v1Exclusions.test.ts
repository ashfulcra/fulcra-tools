// @vitest-environment node
import {expect,test,vi} from "vitest";
import {buildV1ExclusionRecord,exclusionIdFor,type V1ExclusionInput} from "../../src/relayless/v1Exclusions";
import fixtures from "../../../tests/fixtures/v1_exclusion_cases.json";
for(const fixture of fixtures.cases)test("Python/browser permanent policy identity and fields: "+fixture.name,async()=>{
 const row=await buildV1ExclusionRecord(fixture.input);expect(row).toEqual(fixture.record);
 expect(await exclusionIdFor(fixture.input.controlType,fixture.input.targetDataType,fixture.input.targetRecordId)).toBe(row.id);
 expect(Object.isFrozen(row)).toBe(true);for(const key of ["sources","tags","evidence"] as const)expect(Object.isFrozen(row[key])).toBe(true);
});
test("all policy inputs are captured before the first asynchronous digest",async()=>{
 const input=structuredClone(fixtures.cases[1].input),pending=buildV1ExclusionRecord(input);
 input.reason="operator";input.evidence.length=0;input.targetRecordId="11111111-2222-4333-8444-555555555555";
 expect(await pending).toEqual(fixtures.cases[1].record);
});
for(const [name,change] of Object.entries({legacy:{controlType:"MomentAnnotation/synthetic"},metricControl:{controlType:fixtures.cases[1].input.targetDataType},
 self:{targetDataType:fixtures.cases[0].input.controlType},badTarget:{targetDataType:"Event"},badId:{targetRecordId:"not-a-uuid"},
 badAction:{actionId:"not-a-uuid"},restore:{reason:"restore"},unknown:{extra:"unknown"},emptyEvidence:{evidence:[""]},
 badEvidence:{evidence:[false]},lossyUnicode:{evidence:["\ud800"]},largeEvidence:{evidence:Array(129).fill("synthetic")},
 largeReference:{evidence:["x".repeat(1025)]},precision:{requestedAt:"2026-01-01T00:00:00.1234567Z"},
 missingZone:{requestedAt:"2026-01-01T00:00:00"}}))test("unsupported exclusion input holds: "+name,async()=>{
 await expect(buildV1ExclusionRecord({...fixtures.cases[0].input,...change} as unknown as V1ExclusionInput)).rejects.toThrow();
});
test("policy input accessors are refused without execution",async()=>{
 const getter=vi.fn(()=>"synthetic"),input=Object.defineProperty({...fixtures.cases[0].input},"reason",{enumerable:true,get:getter});
 await expect(buildV1ExclusionRecord(input)).rejects.toThrow();expect(getter).not.toHaveBeenCalled();
});
