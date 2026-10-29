// Standalone synthetic parity: compile with Wire.swift and V1Wire.swift.
import Foundation

@main
struct V1WireParityCheck {
    static func main() throws {
        let fixtures = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))) as! [String: Any]
        let cases = fixtures["cases"] as! [[String: Any]]
        var checked = 0
        func optional(_ row: [String: Any], _ key: String) -> String? { row[key] as? String }
        func event(_ row: [String: Any]) -> AttentionEvent {
            AttentionEvent(url: optional(row, "url"), title: optional(row, "title"),
                ogDescription: optional(row, "og_description"), faviconURL: optional(row, "favicon_url"),
                category: optional(row, "category"), chromeIdentity: optional(row, "chrome_identity"),
                ogType: optional(row, "og_type"), lang: optional(row, "lang"),
                startTime: row["start_time"] as! String, endTime: row["end_time"] as! String, client: row["client"] as! String)
        }
        func context(_ row: [String: Any]) -> V1WireContext {
            V1WireContext(dataType: row["dataType"] as! String, tagIds: row["tagIds"] as! [String],
                identitySlug: row["identitySlug"] as! String, identityLabel: optional(row, "identityLabel"))
        }
        for fixture in cases {
            let built = try AttentionV1Wire.buildRecord(event: event(fixture["event"] as! [String: Any]),
                context: context(fixture["context"] as! [String: Any]))
            guard NSDictionary(dictionary: built.record).isEqual(to: fixture["record"] as! [String: Any]),
                  built.sourceId == fixture["source_id"] as! String else {
                fatalError("Attention v1 synthetic fixture mismatch")
            }
            checked += 1
        }
        let first = cases[0]
        let original = event(first["event"] as! [String: Any])
        let ctx = context(first["context"] as! [String: Any])
        func refuses(_ candidate: AttentionEvent, _ context: V1WireContext) {
            do {
                _ = try AttentionV1Wire.buildRecord(event: candidate, context: context)
                fatalError("Attention v1 accepted an invalid synthetic input")
            } catch { checked += 1 }
        }
        for timestamp in ["2026-05-18T14:00:00", "2026-02-30T14:00:00Z", "2026-05-18T14:00:00+24:00",
                          "2026-05-18T14:00:00.000000001Z", "2026-05-18T14:00:00+01:60"] {
            var bad = original
            bad.startTime = timestamp
            refuses(bad, ctx)
        }
        for rawURL in ["https://example.com:443/a", "https://EXAMPLE.com/a", "https://example.com/a/../b",
                       "https://example.com/%2e/b", "https://example.com/a?token=secret", "https://example.com/a#fragment",
                       "https://example.com/a?x=%FF", "https://127.1/a", "https://0x7f000001/a", "https://example.com/a?x=%41",
                       "https://example.com/a?x=a&&y=b", "https://[::1]/a", "https://example.com/a?x=%zz", "https://0x/a", "https://example.0x/a", "https://xn--a/a"] {
            var bad = original
            bad.url = rawURL
            refuses(bad, ctx)
        }
        do {
            _ = try AttentionV1Wire.milliseconds("9999-12-31T23:59:59.999-00:01")
            fatalError("Attention v1 accepted a year 10000 UTC rollover")
        } catch { checked += 1 }
        guard try AttentionV1Wire.milliseconds("9999-12-31T23:59:59.999Z") == 253402300799999 else {
            fatalError("Attention v1 refused supported last millisecond")
        }
        checked += 1
        var reversed = original
        reversed.endTime = "2020-01-01T00:00:00Z"
        refuses(reversed, ctx)
        var twoVariants = original
        twoVariants.category = "Work"
        refuses(twoVariants, ctx)
        var missingVariant = original
        missingVariant.url = nil
        refuses(missingVariant, ctx)
        for invalid in ["Event", "DurationAnnotation/11111111-2222-4333-8444-555555555555"] {
            var bad = ctx
            bad.dataType = invalid
            refuses(original, bad)
        }
        var badTag = ctx
        badTag.tagIds = ["tag-placeholder"]
        refuses(original, badTag)
        var noIdentity = ctx
        noIdentity.identitySlug = ""
        refuses(original, noIdentity)
        print("Attention v1 synthetic parity/refusal checks passed: \(checked)")
    }
}
