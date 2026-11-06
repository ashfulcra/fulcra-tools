// @vitest-environment node
import {expect,test} from "vitest";
import {parseV1Instant,formatV1Instant} from "../../src/relayless/v1Time";
import {attentionTime} from "../../src/relayless/v1Wire";
import {v1RecordMatches} from "../../src/relayless/v1Records";
import fixtures from "../../../tests/fixtures/v1_exclusion_cases.json";
for(const fixture of fixtures.cases)test("lossless UTC/Python formatting: "+fixture.name,()=>{
 const instant=parseV1Instant(fixture.input.requestedAt);expect(formatV1Instant(instant)).toBe(fixture.record.start_time);
 expect(parseV1Instant(fixture.record.start_time)).toBe(instant);
 expect(v1RecordMatches({start_time:fixture.input.requestedAt},{start_time:fixture.record.start_time})).toBe(true);
});
for(const value of ["2026-01-02T00:00:00.123456Z","1969-12-31T23:59:59.999999Z"])test("Attention retains millisecond-only capture bounds: "+value,()=>{
 expect(()=>attentionTime(value)).toThrow();
});
for(const value of ["", "2026-01-01", "2026-01-01T00:00:00", "2026-02-29T00:00:00Z", "2000-02-30T00:00:00Z",
 "2026-01-01T24:00:00Z","2026-01-01T00:60:00Z","2026-01-01T00:00:60Z","2026-01-01T00:00:00+24:00",
 "2026-01-01T00:00:00+00:60","0000-01-01T00:00:00Z","0001-01-01T00:00:00+00:01",
 "9999-12-31T23:59:59.999999-00:01","2026-01-01T00:00:00.1234567Z","2026-01-01T00:00:00Z\n"])
 test("refuses invalid/lossy timestamp: "+value,()=>{expect(()=>parseV1Instant(value)).toThrow();});
test("negative-epoch microseconds and equivalent nonzero offsets remain exact",()=>{
 expect(parseV1Instant("1969-12-31T23:59:59.999999Z")).toBe(-1n);
 expect(formatV1Instant(-1n)).toBe("1969-12-31T23:59:59.999999Z");
 expect(parseV1Instant("2026-01-01T00:00:00.123456000Z")).toBe(parseV1Instant("2026-01-01T00:00:00.123456Z"));
 expect(attentionTime("2026-01-01T00:00:00.123000Z")).toBe(Number(parseV1Instant("2026-01-01T00:00:00.123000Z")/1000n));
 expect(v1RecordMatches({start_time:"2026-01-01T00:00:00.123457Z"},{start_time:"2026-01-01T00:00:00.123456Z"})).toBe(false);
});
for(const value of [-62135596800000001n,253402300800000000n])test("UTC formatter refuses out-of-range instant",()=>{
 expect(()=>formatV1Instant(value)).toThrow();
});
