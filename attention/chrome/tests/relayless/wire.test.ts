// chrome/tests/relayless/wire.test.ts
//
// Golden self-consistency tests for the event->ingest-record transform. The
// daemon (and its Python transform) is gone, so these are no longer a
// cross-language parity check: they pin the TS output of the v3 source_id and
// the inner data string. The expected source_ids were computed by hashing
//   sha256(`${key}|${sec}|${identitySlug}`)[:16]
// with a node one-liner (see this file's vectors) — recompute the same way if
// the formula ever changes. v3 folds the per-browser identity slug into the
// source_id, so two browsers' records on the same url+second stay distinct.

import { describe, test, expect } from "vitest";
import { buildWireRecord, sourceId, encodeBatch } from "../../src/relayless/wire";
import type { AttentionEvent } from "../../src/types";

// No identity (empty slug) context — the base vectors.
const CTX = {
  definitionId: "def-attn-123",
  tagIds: ["tag-attn", "tag-web"],
  identitySlug: "",
};
// A context carrying a per-browser identity.
const CTX_IDENTITY = {
  definitionId: "def-attn-123",
  tagIds: ["tag-attn", "tag-web", "tag-machine"],
  identitySlug: "work-mbp-chrome",
  identityLabel: "Work MBP — Chrome",
};

function urlEvent(): AttentionEvent {
  return {
    url: "https://example.com/article?id=42&utm_source=newsletter#section",
    title: "Example Article",
    og_description: "A description",
    favicon_url: "https://example.com/favicon.ico",
    category: null,
    chrome_identity: null,
    og_type: "article",
    lang: "en",
    start_time: "2026-05-18T14:00:00.500Z",
    end_time: "2026-05-18T14:05:00.900Z",
    client: "fulcra-attention-chrome/0.1.0",
  };
}

describe("source_id v3 golden vectors", () => {
  test("url variant, NO identity (empty slug)", async () => {
    // sha256("https://example.com/article?id=42|2026-05-18T14:00:00+00:00|")[:16]
    // scrub drops utm_source + fragment -> https://example.com/article?id=42
    const sid = await sourceId(
      "https://example.com/article?id=42",
      "2026-05-18T14:00:00.500Z",
      "",
    );
    expect(sid).toBe("com.fulcra.attention.v3.5996501602844de6");
  });

  test("url variant WITH identity slug differs from the no-identity vector", async () => {
    const sid = await sourceId(
      "https://example.com/article?id=42",
      "2026-05-18T14:00:00.500Z",
      "work-mbp-chrome",
    );
    expect(sid).toBe("com.fulcra.attention.v3.29a1df25d7b52081");
  });

  test("category variant, NO identity", async () => {
    const sid = await sourceId("Work", "2026-05-18T14:00:00Z", "");
    expect(sid).toBe("com.fulcra.attention.v3.4cf67b2597b643d3");
  });

  test("source_id is computed over the SCRUBBED url via buildWireRecord", async () => {
    const { sourceId: sid } = await buildWireRecord(urlEvent(), CTX);
    expect(sid).toBe("com.fulcra.attention.v3.5996501602844de6");
  });

  test("same url+second, DIFFERENT identity slugs → DIFFERENT source_ids (multi-browser guarantee)", async () => {
    const a = await sourceId(
      "https://example.com/article?id=42",
      "2026-05-18T14:00:00.500Z",
      "work-mbp-chrome",
    );
    const b = await sourceId(
      "https://example.com/article?id=42",
      "2026-05-18T14:00:00.500Z",
      "home-imac",
    );
    expect(a).not.toBe(b);
    expect(a).toBe("com.fulcra.attention.v3.29a1df25d7b52081");
    expect(b).toBe("com.fulcra.attention.v3.4c345e6c6503d02e");
  });

  test("buildWireRecord with an identity context uses the identity-folded source_id", async () => {
    const { sourceId: sid } = await buildWireRecord(urlEvent(), CTX_IDENTITY);
    expect(sid).toBe("com.fulcra.attention.v3.29a1df25d7b52081");
  });
});

describe("wire record shape (url variant, no identity)", () => {
  test("full record", async () => {
    const { record } = await buildWireRecord(urlEvent(), CTX);
    expect(record.specversion).toBe(1);
    expect(record.metadata.data_type).toBe("DurationAnnotation");
    expect(record.metadata.content_type).toBe("application/json");
    expect(record.metadata.recorded_at).toEqual({
      start_time: "2026-05-18T14:00:00Z",
      end_time: "2026-05-18T14:05:00Z",
    });
    expect(record.metadata.source).toEqual([
      "com.fulcra.attention.v3.5996501602844de6",
      "com.fulcradynamics.annotation.def-attn-123",
    ]);
    expect(record.metadata.tags).toEqual(["tag-attn", "tag-web"]);
  });

  test("inner data string is the sorted-key JSON (device/device_label null when no identity)", async () => {
    const { record } = await buildWireRecord(urlEvent(), CTX);
    const expected =
      '{"category": null, "duration_seconds": 300, "external_ids": ' +
      '{"chrome_identity": null, "client": "fulcra-attention-chrome/0.1.0", ' +
      '"device": null, "device_label": null, ' +
      '"host": "example.com", "lang": "en", "og_type": "article"}, ' +
      '"favicon_url": "https://example.com/favicon.ico", ' +
      '"note": "Example Article \\u2014 https://example.com/article?id=42", ' +
      '"og_description": "A description", "parent_source_id": null, ' +
      '"service": "web", "title": "Example Article", ' +
      '"url": "https://example.com/article?id=42"}';
    expect(record.data).toBe(expected);
  });
});

describe("external_ids carries device + device_label", () => {
  test("identity context → device=slug, device_label=raw label, and machine tag passes through", async () => {
    const { record } = await buildWireRecord(urlEvent(), CTX_IDENTITY);
    expect(record.data).toContain('"device": "work-mbp-chrome"');
    expect(record.data).toContain('"device_label": "Work MBP \\u2014 Chrome"');
    expect(record.metadata.tags).toEqual(["tag-attn", "tag-web", "tag-machine"]);
  });

  test("no identity → device + device_label both null", async () => {
    const { record } = await buildWireRecord(urlEvent(), CTX);
    expect(record.data).toContain('"device": null');
    expect(record.data).toContain('"device_label": null');
  });
});

describe("wire record shape (category variant)", () => {
  test("inner data + source", async () => {
    const ev: AttentionEvent = {
      url: null,
      title: null,
      og_description: null,
      favicon_url: null,
      category: "Work",
      chrome_identity: null,
      og_type: null,
      lang: null,
      start_time: "2026-05-18T14:00:00Z",
      end_time: "2026-05-18T14:05:00Z",
      client: "fulcra-attention-chrome/0.1.0",
    };
    const { record, sourceId: sid } = await buildWireRecord(ev, CTX);
    const expected =
      '{"category": "Work", "duration_seconds": 300, "external_ids": ' +
      '{"chrome_identity": null, "client": "fulcra-attention-chrome/0.1.0", ' +
      '"device": null, "device_label": null, ' +
      '"host": null, "lang": null, "og_type": null}, "favicon_url": null, ' +
      '"note": "Attention: Work", "og_description": null, ' +
      '"parent_source_id": null, "service": "web", "title": null, "url": null}';
    expect(record.data).toBe(expected);
    expect(sid).toBe("com.fulcra.attention.v3.4cf67b2597b643d3");
    expect(record.metadata.source[0]).toBe(sid);
  });
});

describe("non-ASCII parity (ensure_ascii)", () => {
  test("inner data escapes non-ASCII to \\uXXXX like Python", async () => {
    // Uses ASCII host but non-ASCII title so the data-string escaping is
    // exercised without depending on JS-vs-Python IDN host normalization.
    const ev: AttentionEvent = {
      url: "https://example.com/p",
      title: "日本語 タイトル",
      og_description: null,
      favicon_url: null,
      category: null,
      chrome_identity: null,
      og_type: null,
      lang: null,
      start_time: "2026-05-18T14:00:00Z",
      end_time: "2026-05-18T14:05:00Z",
      client: "c",
    };
    const { record } = await buildWireRecord(ev, CTX);
    // The em-dash and the Japanese codepoints must be \uXXXX-escaped.
    expect(record.data).toContain(
      '"note": "\\u65e5\\u672c\\u8a9e \\u30bf\\u30a4\\u30c8\\u30eb \\u2014 https://example.com/p"',
    );
    expect(record.data).toContain(
      '"title": "\\u65e5\\u672c\\u8a9e \\u30bf\\u30a4\\u30c8\\u30eb"',
    );
  });
});

describe("encodeBatch", () => {
  test("newline-joined sorted-key JSON, one record per line", async () => {
    const { record: r1 } = await buildWireRecord(urlEvent(), CTX);
    const { record: r2 } = await buildWireRecord(urlEvent(), CTX);
    const body = encodeBatch([r1, r2]);
    const lines = body.split("\n");
    expect(lines).toHaveLength(2);
    // Outer keys sorted: data < metadata < specversion.
    expect(lines[0].startsWith('{"data": ')).toBe(true);
    expect(lines[0]).toContain('"specversion": 1');
    // The inner data string is escaped (quotes backslash-escaped).
    expect(lines[0]).toContain('\\"category\\": null');
  });
});

describe("duration clamps at zero", () => {
  test("misordered end<start yields duration_seconds 0", async () => {
    const ev = urlEvent();
    ev.start_time = "2026-05-18T14:05:00Z";
    ev.end_time = "2026-05-18T14:00:00Z";
    const { record } = await buildWireRecord(ev, CTX);
    expect(record.data).toContain('"duration_seconds": 0');
  });
});
