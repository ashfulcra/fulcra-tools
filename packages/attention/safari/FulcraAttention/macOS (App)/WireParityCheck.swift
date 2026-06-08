// macOS (App)/WireParityCheck.swift
//
// Standalone parity check for Wire.swift against the golden vectors in
// packages/attention/chrome/tests/relayless/wire.test.ts. Run it by compiling it
// together with Wire.swift, e.g.:
//
//   swiftc -parse-as-library \
//     "macOS (App)/Wire.swift" "macOS (App)/WireParityCheck.swift" \
//     -o /tmp/wire-parity && /tmp/wire-parity
//
// (It is marked @main so it provides the entry point; it is NOT part of any
// app target build — Wire.swift is. The macOS app target excludes this file.)
//
// Every (key, start, slug) -> expected source_id pair below is copied verbatim
// from wire.test.ts. The data-string vectors are copied from the same file.
// PASS/FAIL is printed per case and the process exits non-zero on any FAIL.

import Foundation

@main
struct WireParityCheck {
    static var failures = 0
    static var total = 0

    static func check(_ name: String, _ got: String, _ want: String) {
        total += 1
        if got == want {
            print("PASS  \(name)")
        } else {
            failures += 1
            print("FAIL  \(name)")
            print("        expected: \(want)")
            print("        got:      \(got)")
        }
    }

    static func urlEvent() -> AttentionEvent {
        AttentionEvent(
            url: "https://example.com/article?id=42&utm_source=newsletter#section",
            title: "Example Article",
            ogDescription: "A description",
            faviconURL: "https://example.com/favicon.ico",
            category: nil,
            chromeIdentity: nil,
            ogType: "article",
            lang: "en",
            startTime: "2026-05-18T14:00:00.500Z",
            endTime: "2026-05-18T14:05:00.900Z",
            client: "fulcra-attention-chrome/0.1.0"
        )
    }

    static let CTX = WireContext(
        definitionId: "def-attn-123",
        tagIds: ["tag-attn", "tag-web"],
        identitySlug: ""
    )
    static let CTX_IDENTITY = WireContext(
        definitionId: "def-attn-123",
        tagIds: ["tag-attn", "tag-web", "tag-machine"],
        identitySlug: "work-mbp-chrome",
        identityLabel: "Work MBP — Chrome"
    )

    static func main() {
        // ---- source_id v3 golden vectors (from wire.test.ts) ----

        check(
            "sourceId: url variant, NO identity (empty slug)",
            Wire.sourceId(
                key: "https://example.com/article?id=42",
                startTimeISO: "2026-05-18T14:00:00.500Z",
                identitySlug: ""
            ) ?? "<nil>",
            "com.fulcra.attention.v3.5996501602844de6"
        )

        check(
            "sourceId: url variant WITH identity slug work-mbp-chrome",
            Wire.sourceId(
                key: "https://example.com/article?id=42",
                startTimeISO: "2026-05-18T14:00:00.500Z",
                identitySlug: "work-mbp-chrome"
            ) ?? "<nil>",
            "com.fulcra.attention.v3.29a1df25d7b52081"
        )

        check(
            "sourceId: category variant, NO identity (Work @ 14:00:00Z)",
            Wire.sourceId(
                key: "Work",
                startTimeISO: "2026-05-18T14:00:00Z",
                identitySlug: ""
            ) ?? "<nil>",
            "com.fulcra.attention.v3.4cf67b2597b643d3"
        )

        check(
            "sourceId: url variant WITH identity slug home-imac",
            Wire.sourceId(
                key: "https://example.com/article?id=42",
                startTimeISO: "2026-05-18T14:00:00.500Z",
                identitySlug: "home-imac"
            ) ?? "<nil>",
            "com.fulcra.attention.v3.4c345e6c6503d02e"
        )

        // ---- buildWireRecord source_id (computed over SCRUBBED url) ----

        do {
            let r = try Wire.buildWireRecord(event: urlEvent(), context: CTX)
            check(
                "buildWireRecord: source_id over scrubbed url (no identity)",
                r.sourceId,
                "com.fulcra.attention.v3.5996501602844de6"
            )
        } catch {
            check("buildWireRecord: source_id over scrubbed url (no identity)",
                  "threw \(error)", "no-throw")
        }

        do {
            let r = try Wire.buildWireRecord(event: urlEvent(), context: CTX_IDENTITY)
            check(
                "buildWireRecord: identity context uses identity-folded source_id",
                r.sourceId,
                "com.fulcra.attention.v3.29a1df25d7b52081"
            )
        } catch {
            check("buildWireRecord: identity context uses identity-folded source_id",
                  "threw \(error)", "no-throw")
        }

        // ---- inner data string parity (url variant, no identity) ----

        let expectedDataNoIdentity =
            "{\"category\": null, \"duration_seconds\": 300, \"external_ids\": " +
            "{\"chrome_identity\": null, \"client\": \"fulcra-attention-chrome/0.1.0\", " +
            "\"device\": null, \"device_label\": null, " +
            "\"host\": \"example.com\", \"lang\": \"en\", \"og_type\": \"article\"}, " +
            "\"favicon_url\": \"https://example.com/favicon.ico\", " +
            "\"note\": \"Example Article \\u2014 https://example.com/article?id=42\", " +
            "\"og_description\": \"A description\", \"parent_source_id\": null, " +
            "\"service\": \"web\", \"title\": \"Example Article\", " +
            "\"url\": \"https://example.com/article?id=42\"}"
        do {
            let r = try Wire.buildWireRecord(event: urlEvent(), context: CTX)
            check("data string: url variant, no identity", r.record.data, expectedDataNoIdentity)
            check("recorded_at start", r.record.recordedAtStart, "2026-05-18T14:00:00Z")
            check("recorded_at end", r.record.recordedAtEnd, "2026-05-18T14:05:00Z")
            check("metadata.source[0]", r.record.source[0], "com.fulcra.attention.v3.5996501602844de6")
            check("metadata.source[1]", r.record.source[1], "com.fulcradynamics.annotation.def-attn-123")
            check("metadata.tags", r.record.tags.joined(separator: ","), "tag-attn,tag-web")
            check("metadata.data_type", r.record.dataType, "DurationAnnotation")
        } catch {
            check("data string: url variant, no identity", "threw \(error)", "no-throw")
        }

        // ---- inner data string parity (identity context) ----

        do {
            let r = try Wire.buildWireRecord(event: urlEvent(), context: CTX_IDENTITY)
            check("data contains device=slug",
                  r.record.data.contains("\"device\": \"work-mbp-chrome\"") ? "yes" : "no", "yes")
            check("data contains device_label (em-dash escaped)",
                  r.record.data.contains("\"device_label\": \"Work MBP \\u2014 Chrome\"") ? "yes" : "no", "yes")
            check("identity tags pass through",
                  r.record.tags.joined(separator: ","), "tag-attn,tag-web,tag-machine")
        } catch {
            check("data string: identity context", "threw \(error)", "no-throw")
        }

        // ---- category variant data string + source_id ----

        let catEvent = AttentionEvent(
            url: nil, title: nil, ogDescription: nil, faviconURL: nil,
            category: "Work", chromeIdentity: nil, ogType: nil, lang: nil,
            startTime: "2026-05-18T14:00:00Z", endTime: "2026-05-18T14:05:00Z",
            client: "fulcra-attention-chrome/0.1.0"
        )
        let expectedCatData =
            "{\"category\": \"Work\", \"duration_seconds\": 300, \"external_ids\": " +
            "{\"chrome_identity\": null, \"client\": \"fulcra-attention-chrome/0.1.0\", " +
            "\"device\": null, \"device_label\": null, " +
            "\"host\": null, \"lang\": null, \"og_type\": null}, \"favicon_url\": null, " +
            "\"note\": \"Attention: Work\", \"og_description\": null, " +
            "\"parent_source_id\": null, \"service\": \"web\", \"title\": null, \"url\": null}"
        do {
            let r = try Wire.buildWireRecord(event: catEvent, context: CTX)
            check("data string: category variant", r.record.data, expectedCatData)
            check("category source_id", r.sourceId, "com.fulcra.attention.v3.4cf67b2597b643d3")
        } catch {
            check("data string: category variant", "threw \(error)", "no-throw")
        }

        // ---- non-ASCII parity (ensure_ascii) ----

        let nonAsciiEvent = AttentionEvent(
            url: "https://example.com/p",
            title: "日本語 タイトル",
            ogDescription: nil, faviconURL: nil, category: nil,
            chromeIdentity: nil, ogType: nil, lang: nil,
            startTime: "2026-05-18T14:00:00Z", endTime: "2026-05-18T14:05:00Z",
            client: "c"
        )
        do {
            let r = try Wire.buildWireRecord(event: nonAsciiEvent, context: CTX)
            check("non-ASCII note escaped",
                  r.record.data.contains(
                    "\"note\": \"\\u65e5\\u672c\\u8a9e \\u30bf\\u30a4\\u30c8\\u30eb \\u2014 https://example.com/p\""
                  ) ? "yes" : "no", "yes")
            check("non-ASCII title escaped",
                  r.record.data.contains(
                    "\"title\": \"\\u65e5\\u672c\\u8a9e \\u30bf\\u30a4\\u30c8\\u30eb\""
                  ) ? "yes" : "no", "yes")
        } catch {
            check("non-ASCII parity", "threw \(error)", "no-throw")
        }

        // ---- WHATWG URL scrub parity edge cases ----

        let idnEvent = AttentionEvent(
            url: "https://例え.テスト/path?q=a b&utm_source=z#frag",
            title: nil, ogDescription: nil, faviconURL: nil, category: nil,
            chromeIdentity: nil, ogType: nil, lang: nil,
            startTime: "1969-12-31T23:59:59.999Z",
            endTime: "1970-01-01T00:00:00.001Z",
            client: "c"
        )
        do {
            let r = try Wire.buildWireRecord(event: idnEvent, context: CTX)
            check("WHATWG scrub: IDN host punycoded + query space as plus",
                  r.record.data.contains("\"url\": \"https://xn--r8jz45g.xn--zckzah/path?q=a+b\"") ? "yes" : "no",
                  "yes")
            check("WHATWG scrub: IDN host external_id",
                  r.record.data.contains("\"host\": \"xn--r8jz45g.xn--zckzah\"") ? "yes" : "no",
                  "yes")
            check("WHATWG scrub: IDN source_id",
                  r.sourceId,
                  "com.fulcra.attention.v3.cd88486424c6ac2b")
        } catch {
            check("WHATWG scrub: IDN edge", "threw \(error)", "no-throw")
        }

        let escapedEvent = AttentionEvent(
            url: "https://example.com/%7Euser?q=%7E&keep=a%2Bb&email=x@y",
            title: nil, ogDescription: nil, faviconURL: nil, category: nil,
            chromeIdentity: nil, ogType: nil, lang: nil,
            startTime: "1969-12-31T23:59:59.999Z",
            endTime: "1970-01-01T00:00:00.001Z",
            client: "c"
        )
        do {
            let r = try Wire.buildWireRecord(event: escapedEvent, context: CTX)
            check("WHATWG scrub: preserves encoded path and literal plus",
                  r.record.data.contains("\"url\": \"https://example.com/%7Euser?q=%7E&keep=a%2Bb\"") ? "yes" : "no",
                  "yes")
            check("WHATWG scrub: encoded source_id",
                  r.sourceId,
                  "com.fulcra.attention.v3.c85b4e764d17f2c3")
        } catch {
            check("WHATWG scrub: encoded edge", "threw \(error)", "no-throw")
        }

        // ---- encodeBatch ----

        do {
            let r1 = try Wire.buildWireRecord(event: urlEvent(), context: CTX)
            let r2 = try Wire.buildWireRecord(event: urlEvent(), context: CTX)
            let body = Wire.encodeBatch([r1.record, r2.record])
            let lines = body.split(separator: "\n", omittingEmptySubsequences: false)
            check("encodeBatch line count", String(lines.count), "2")
            check("encodeBatch line0 starts with data",
                  lines[0].hasPrefix("{\"data\": ") ? "yes" : "no", "yes")
            check("encodeBatch line0 has specversion",
                  lines[0].contains("\"specversion\": 1") ? "yes" : "no", "yes")
            check("encodeBatch inner data quotes escaped",
                  lines[0].contains("\\\"category\\\": null") ? "yes" : "no", "yes")
        } catch {
            check("encodeBatch", "threw \(error)", "no-throw")
        }

        // ---- duration clamps at zero ----

        var misordered = urlEvent()
        misordered.startTime = "2026-05-18T14:05:00Z"
        misordered.endTime = "2026-05-18T14:00:00Z"
        do {
            let r = try Wire.buildWireRecord(event: misordered, context: CTX)
            check("duration clamps at zero",
                  r.record.data.contains("\"duration_seconds\": 0") ? "yes" : "no", "yes")
        } catch {
            check("duration clamps at zero", "threw \(error)", "no-throw")
        }

        // ---- summary ----
        print("")
        print("==== \(total - failures)/\(total) PASS, \(failures) FAIL ====")
        if failures > 0 {
            exit(1)
        }
        print("ALL GOLDEN VECTORS MATCHED")
    }
}
