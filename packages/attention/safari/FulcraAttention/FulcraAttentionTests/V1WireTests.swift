import XCTest
@testable import FulcraAttention

final class V1WireTests: XCTestCase {
    func testSharedFlatEventVectors() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf:
            root.appendingPathComponent("tests/fixtures/v1_wire_cases.json"))) as! [String: Any]
        for item in fixture["cases"] as! [[String: Any]] {
            let raw = item["event"] as! [String: Any], context = item["context"] as! [String: Any]
            let event = AttentionEvent(url: raw["url"] as? String, title: raw["title"] as? String,
                ogDescription: raw["og_description"] as? String, faviconURL: raw["favicon_url"] as? String,
                category: raw["category"] as? String, chromeIdentity: raw["chrome_identity"] as? String,
                ogType: raw["og_type"] as? String, lang: raw["lang"] as? String,
                startTime: raw["start_time"] as! String, endTime: raw["end_time"] as! String, client: raw["client"] as! String)
            let built = try AttentionV1Wire.buildRecord(event: event, context: V1WireContext(
                dataType: context["dataType"] as! String, tagIds: context["tagIds"] as! [String],
                identitySlug: context["identitySlug"] as! String, identityLabel: context["identityLabel"] as? String))
            XCTAssertEqual(built.sourceId, item["source_id"] as? String)
            XCTAssertTrue(NSDictionary(dictionary: built.record).isEqual(to: item["record"] as! [String: Any]))
        }
    }

    func testTimeRejectsInvalidCalendarTimezoneAndPrecision() {
        for invalid in ["2026-02-30T00:00:00Z", "2026-01-01T00:00:00", "2026-01-01T00:00:00+24:00",
                        "2026-01-01T00:00:00.0001Z", "2026-01-01T00:00:00+01:60", "2026-01-01T00:00:00Z\n", "9999-12-31T23:59:59.999-00:01"] {
            XCTAssertThrowsError(try AttentionV1Wire.milliseconds(invalid))
        }
        XCTAssertEqual(try AttentionV1Wire.milliseconds("9999-12-31T23:59:59.999Z"), 253402300799999)
        XCTAssertEqual(try AttentionV1Wire.milliseconds("1969-12-31T23:59:59.500Z"), -500)
    }
}
