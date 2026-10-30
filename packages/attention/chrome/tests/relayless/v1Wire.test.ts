import { describe, expect, test } from "vitest";
import fixtures from "../../../tests/fixtures/v1_wire_cases.json";
import { attentionTime, attentionV1Contract, buildV1Record, recordIdFor } from "../../src/relayless/v1Wire";

describe("shared Attention v1 contract", () => {
  test("contract snapshots cannot mutate subsequent destination schemas", () => {
    const copy = attentionV1Contract();
    copy.fields.note.type = "number";
    expect(attentionV1Contract().fields.note.type).toBe("string");
  });
  test("inputs cannot change across asynchronous hashing", async () => {
    const fixture = fixtures.cases[0];
    const event = { ...fixture.event };
    const context = { ...fixture.context, tagIds: [...fixture.context.tagIds] };
    const pending = buildV1Record(event, context);
    event.title = "Changed after capture";
    context.identitySlug = "different-installation";
    context.tagIds.length = 0;
    expect((await pending).record).toEqual(fixture.record);
  });
  for (const fixture of fixtures.cases) {
    test(fixture.name, async () => {
      const built = await buildV1Record(fixture.event, fixture.context);
      expect(built.sourceId).toBe(fixture.source_id);
      expect(built.record).toEqual(fixture.record);
      expect(await recordIdFor(fixture.context.dataType, fixture.source_id)).toBe(fixture.record.id);
      expect(built.record).not.toHaveProperty("metadata");
      expect(built.record).not.toHaveProperty("data");
      expect(built.record).not.toHaveProperty("specversion");
    });
  }
  test("UTC rollover beyond year 9999 is refused", () => {
    expect(() => attentionTime("9999-12-31T23:59:59.999-00:01")).toThrow();
    expect(attentionTime("9999-12-31T23:59:59.999Z")).toBe(253402300799999);
  });
  for (const url of ["https://example.com:443/a", "https://EXAMPLE.com/a", "https://example.com/a/../b",
    "https://example.com/%2e/b", "https://example.com/a?token=secret", "https://example.com/a#fragment",
    "https://example.com/a?x=%FF", "https://127.1/a", "https://0x7f000001/a", "https://example.com/a?x=%41",
    "https://example.com/a?x=a&&y=b", "https://[::1]/a", "https://example.com/a?x=%zz", "https://0x/a", "https://example.0x/a", "https://xn--a/a"] ) {
    test(`refuses a URL outside the prepared capture boundary: ${url}`, async () => {
      await expect(buildV1Record({ ...fixtures.cases[0].event, url }, fixtures.cases[0].context)).rejects.toThrow();
    });
  }
  for (const [name, change] of Object.entries({
    naive: { start_time: "2026-05-18T14:00:00" },
    trailingNewline: { start_time: "2026-05-18T14:00:00Z\n" },
    invalidDay: { start_time: "2026-02-30T14:00:00Z" },
    invalidOffset: { start_time: "2026-05-18T14:00:00+24:00" },
    excessPrecision: { start_time: "2026-05-18T14:00:00.000000001Z" },
    reversed: { end_time: "2020-01-01T00:00:00Z" },
    missingVariant: { url: null },
    twoVariants: { category: "Work" },
    invalidUnicode: { title: "\ud800" },
    unknown: { unknown_field: "must remain held" },
  })) {
    test(`rejects ${name}`, async () => {
      const fixture = fixtures.cases[0];
      await expect(buildV1Record({ ...fixture.event, ...change }, fixture.context)).rejects.toThrow();
    });
  }
  for (const [name, change] of Object.entries({
    legacyDestination: { dataType: "DurationAnnotation/11111111-2222-4333-8444-555555555555" },
    partialDestination: { dataType: "Event" },
    invalidTag: { tagIds: ["tag-placeholder"] },
    missingIdentity: { identitySlug: "" },
    newlineIdentity: { identitySlug: "fixture-browser-a\n" },
    newlineDestination: { dataType: "Event/11111111-2222-4333-8444-555555555555\n" },
    newlineTag: { tagIds: ["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee\n"] },
  })) {
    test(`rejects ${name}`, async () => {
      const fixture = fixtures.cases[0];
      await expect(buildV1Record(fixture.event, { ...fixture.context, ...change })).rejects.toThrow();
    });
  }
});
