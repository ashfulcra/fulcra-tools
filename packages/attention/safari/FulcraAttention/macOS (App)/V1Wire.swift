// Opt-in flat Event transform. Destination/account/queue/delivery authority is
// separate; importing this file does not switch the installed native sender.
import Foundation
import CryptoKit

public struct V1WireContext {
    public var dataType: String
    public var tagIds: [String]
    public var identitySlug: String
    public var identityLabel: String?

    public init(dataType: String, tagIds: [String], identitySlug: String, identityLabel: String? = nil) {
        self.dataType = dataType
        self.tagIds = tagIds
        self.identitySlug = identitySlug
        self.identityLabel = identityLabel
    }
}

public struct V1WireResult {
    public let record: [String: Any]
    public let sourceId: String
}

public enum AttentionV1Wire {
    public enum Invalid: Error { case destination, identity, timestamp, bounds, variant, url }

    private static func canonicalUUID(_ value: String) -> Bool {
        UUID(uuidString: value)?.uuidString.lowercased() == value
    }

    private static func destination(_ value: String) throws {
        guard value.hasPrefix("Event/"), canonicalUUID(String(value.dropFirst(6))) else { throw Invalid.destination }
    }

    /// Strict calendar/offset validation before the legacy, integer-only time
    /// arithmetic. Nonzero submillisecond precision cannot be silently rounded.
    public static func milliseconds(_ value: String) throws -> Int {
        let pattern = #"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})$"#
        let regex = try NSRegularExpression(pattern: pattern)
        let input = value as NSString
        guard let match = regex.firstMatch(in: value, range: NSRange(location: 0, length: input.length)),
              match.range.length == input.length else { throw Invalid.timestamp }
        func group(_ index: Int) -> String {
            let range = match.range(at: index)
            return range.location == NSNotFound ? "" : input.substring(with: range)
        }
        let parts = (1...6).map { Int(group($0))! }
        let (year, month, day, hour, minute, second) = (parts[0], parts[1], parts[2], parts[3], parts[4], parts[5])
        guard year >= 1, (1...12).contains(month), (0...23).contains(hour),
              (0...59).contains(minute), (0...59).contains(second) else { throw Invalid.timestamp }
        let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
        let days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        guard (1...days[month - 1]).contains(day) else { throw Invalid.timestamp }
        var fraction = group(7)
        while fraction.last == "0" { fraction.removeLast() }
        guard fraction.count <= 3 else { throw Invalid.timestamp }
        let zone = group(8)
        if zone != "Z" {
            let chars = Array(zone)
            guard let hour = Int(String(chars[1...2])), let minute = Int(String(chars[4...5])),
                  hour <= 23, minute <= 59 else { throw Invalid.timestamp }
        }
        guard let result = WireTime.parseISOMilliseconds(value) else { throw Invalid.timestamp }
        // Compare complete epoch bounds: prefix(4) misreads year 10000 as 1000.
        guard (-62135596800000...253402300799999).contains(result) else { throw Invalid.timestamp }
        return result
    }

    private static func iso(_ milliseconds: Int) -> String {
        let second = Int(floor(Double(milliseconds) / 1000))
        let fraction = milliseconds - second * 1000
        let whole = WireTime.renderSecondISO(second, suffix: "Z")
        if fraction == 0 { return whole }
        return String(whole.dropLast()) + String(format: ".%03dZ", fraction)
    }

    /// UUID5 URL namespace, shared with fulcra_common.v1.record_id_for.
    public static func recordIdFor(dataType: String, sourceId: String) throws -> String {
        try destination(dataType)
        guard !sourceId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw Invalid.identity }
        let namespace: [UInt8] = [0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1,
                                  0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8]
        var digest = Array(Insecure.SHA1.hash(data: Data(namespace + Array("fulcra-tools:v1:\(dataType):\(sourceId)".utf8))))
        digest[6] = (digest[6] & 0x0f) | 0x50
        digest[8] = (digest[8] & 0x3f) | 0x80
        let hex = digest.prefix(16).map { String(format: "%02x", $0) }.joined()
        let chars = Array(hex)
        return [String(chars[0..<8]), String(chars[8..<12]), String(chars[12..<16]),
                String(chars[16..<20]), String(chars[20..<32])].joined(separator: "-")
    }

    /// Match the prepared JS capture boundary without re-normalizing its key.
    /// Unsupported input is held by callers; never rewrite it into a new source.
    private static func preparedURL(_ value: String) throws -> (String, String) {
        func matches(_ value: String, _ pattern: String) -> Bool {
            guard let regex = try? NSRegularExpression(pattern: pattern) else { return false }
            let range = NSRange(value.startIndex..., in: value)
            return regex.firstMatch(in: value, range: range)?.range == range
        }
        guard value.utf8.allSatisfy({ (0x21...0x7e).contains($0) }) else { throw Invalid.url }
        let pattern = #"^(https?)://([a-z0-9.-]+)(?::([0-9]+))?(/[^?#]*)(?:\?([^#]+))?$"#
        let regex = try NSRegularExpression(pattern: pattern)
        let input = value as NSString
        guard let match = regex.firstMatch(in: value, range: NSRange(location: 0, length: input.length)),
              match.range.length == input.length else { throw Invalid.url }
        func group(_ index: Int) -> String? {
            let range = match.range(at: index)
            return range.location == NSNotFound ? nil : input.substring(with: range)
        }
        let scheme = group(1)!, host = group(2)!, path = group(4)!
        // URLComponents decodes/validates IDNA; URL.host alone accepts invalid A-labels.
        guard URLComponents(string: value)?.host != nil else { throw Invalid.url }
        let labels = host.components(separatedBy: ".")
        guard host.count <= 253, labels.allSatisfy({ $0.count <= 63 && matches($0, #"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"#) }) else { throw Invalid.url }
        if matches(labels.last!, #"^[0-9]+$"#) || matches(labels.last!, #"^0x[0-9a-f]*$"#) {
            guard labels.count == 4, labels.allSatisfy({ matches($0, #"^(0|[1-9][0-9]{0,2})$"#) && Int($0)! <= 255 }) else { throw Invalid.url }
        }
        if let port = group(3) {
            guard matches(port, #"^(0|[1-9][0-9]{0,4})$"#), Int(port)! <= 65535,
                  port != (scheme == "https" ? "443" : "80") else { throw Invalid.url }
        }
        guard matches(path, #"^(?:[a-zA-Z0-9._~!$&'()*+,;=:@/-]|%[0-9a-fA-F]{2})*$"#),
              !path.components(separatedBy: "/").contains(where: {
                  [".", ".."].contains($0.replacingOccurrences(of: "%2e", with: ".", options: .caseInsensitive))
              }) else { throw Invalid.url }
        if let query = group(5) {
            for item in query.components(separatedBy: "&") {
                guard matches(item, #"^(?:[a-zA-Z0-9*_.+%-])*=(?:[a-zA-Z0-9*_.+%-])*$"#),
                      item.replacingOccurrences(of: "+", with: " ").removingPercentEncoding != nil else { throw Invalid.url }
            }
        }
        guard WireScrub.scrubURL(value) == value else { throw Invalid.url }
        return (value, host)
    }

    public static func buildRecord(event: AttentionEvent, context: V1WireContext) throws -> V1WireResult {
        try destination(context.dataType)
        guard context.identitySlug.range(of: #"^[a-z0-9][a-z0-9-]{0,63}$"#, options: .regularExpression) != nil,
              context.tagIds.allSatisfy(canonicalUUID) else { throw Invalid.identity }
        let start = try milliseconds(event.startTime), end = try milliseconds(event.endTime)
        guard end >= start, !event.client.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw Invalid.bounds }
        guard (event.url == nil) != (event.category == nil) else { throw Invalid.variant }
        var url: String? = nil, host: String? = nil
        if let rawURL = event.url {
            let prepared = try preparedURL(rawURL)
            url = prepared.0
            host = prepared.1
        } else if event.category!.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { throw Invalid.variant }
        guard let primary = Wire.sourceId(key: url ?? event.category!, startTimeISO: iso(start),
                                          identitySlug: context.identitySlug) else { throw Invalid.timestamp }
        let identifier = try recordIdFor(dataType: context.dataType, sourceId: primary)
        func nullable(_ value: String?) -> Any { value.map { $0 as Any } ?? NSNull() }
        var tags: [String] = []
        for tag in context.tagIds where !tags.contains(tag) { tags.append(tag) }
        let note = url.map { event.title.map { $0.isEmpty ? url! : "\($0) — \(url!)" } ?? $0 }
            ?? "Attention: \(event.category!)"
        let external: [String: Any] = ["client": event.client, "host": nullable(host),
            "chrome_identity": nullable(event.chromeIdentity), "og_type": nullable(event.ogType),
            "lang": nullable(event.lang), "device": context.identitySlug, "device_label": nullable(context.identityLabel)]
        let record: [String: Any] = ["id": identifier, "start_time": iso(start), "end_time": iso(end),
            "sources": [primary], "tags": tags, "note": note, "title": nullable(event.title), "service": "web",
            "category": nullable(event.category), "url": nullable(url), "og_description": nullable(event.ogDescription),
            "favicon_url": nullable(event.faviconURL), "parent_source_id": NSNull(),
            "duration_seconds": Double(end - start) / 1000, "external_ids": external]
        return V1WireResult(record: record, sourceId: primary)
    }
}
