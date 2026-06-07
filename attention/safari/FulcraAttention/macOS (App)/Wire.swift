// macOS (App)/Wire.swift
//
// Swift port of the relayless Chrome extension's event -> ingest-record
// transform. The Safari NATIVE app performs the Fulcra ingest (the extension
// JS cannot — Origin 403), so this side must build the EXACT same wire record
// the TypeScript extension builds — byte-for-byte identical, especially the
// `source_id` (a sha256-derived idempotency id). If the Swift source_id
// differed from the TS one, records from Safari vs Chrome would not
// dedup/align.
//
// Source of truth (ported, not reinvented):
//   attention/chrome/src/relayless/wire.ts   — SOURCE_PREFIX, sourceId,
//                                               buildWireRecord, encodeBatch
//   attention/chrome/src/relayless/pyjson.ts — the sorted-key, ensure_ascii
//                                               JSON encoder the `data` string
//                                               must match
//   attention/chrome/src/scrub.ts            — Tier-1 URL scrubbing
//   attention/chrome/tests/relayless/wire.test.ts — the GOLDEN VECTORS
//
// This milestone is JUST the pure transform + a parity check. No auth, no
// networking, no Keychain — those are later.

import Foundation
import CryptoKit

// Bumped v2 -> v3: the source_id folds in the per-browser identity slug, so
// the same url+second from two different browsers produces DISTINCT
// source_ids (the multi-browser distinctness guarantee).
public enum Wire {
    public static let sourcePrefix = "com.fulcra.attention.v3."
    public static let dataType = "DurationAnnotation"
}

// MARK: - AttentionEvent

/// The wire event posted to the Fulcra API ingest endpoint by the relayless
/// sender. Matches the TS `AttentionEvent` shape byte-for-byte. Exactly one of
/// {url, category} is non-null.
public struct AttentionEvent {
    public var url: String?
    public var title: String?
    public var ogDescription: String?
    public var faviconURL: String?
    public var category: String?
    public var chromeIdentity: String?
    public var ogType: String?
    public var lang: String?
    public var startTime: String  // ISO 8601 with explicit zone
    public var endTime: String
    public var client: String

    public init(
        url: String?,
        title: String?,
        ogDescription: String?,
        faviconURL: String?,
        category: String?,
        chromeIdentity: String?,
        ogType: String?,
        lang: String?,
        startTime: String,
        endTime: String,
        client: String
    ) {
        self.url = url
        self.title = title
        self.ogDescription = ogDescription
        self.faviconURL = faviconURL
        self.category = category
        self.chromeIdentity = chromeIdentity
        self.ogType = ogType
        self.lang = lang
        self.startTime = startTime
        self.endTime = endTime
        self.client = client
    }
}

// MARK: - WireContext / WireResult

/// Inputs the caller resolves (definition + tags + identity) for a record.
public struct WireContext {
    public var definitionId: String
    /// Resolved tag ids in order: [attention, web, (machine:<slug>?)].
    public var tagIds: [String]
    /// Per-browser identity slug, folded into the source_id. Empty when none.
    public var identitySlug: String
    /// Raw per-browser identity label, surfaced in external_ids.device_label.
    public var identityLabel: String?

    public init(
        definitionId: String,
        tagIds: [String],
        identitySlug: String,
        identityLabel: String? = nil
    ) {
        self.definitionId = definitionId
        self.tagIds = tagIds
        self.identitySlug = identitySlug
        self.identityLabel = identityLabel
    }
}

/// The result of transforming one event.
public struct WireResult {
    /// The record dict to place in the /ingest/v1/record/batch body.
    public let record: WireRecord
    /// The event's attention source_id — used for dedup by the sender.
    public let sourceId: String
}

/// The wire record. `data` is the json.dumps(data, sort_keys=True) string —
/// byte-identical to the TS extension's `pyJsonStringify`.
public struct WireRecord {
    public let specversion: Int  // always 1
    public let data: String
    public let dataType: String
    public let recordedAtStart: String
    public let recordedAtEnd: String
    public let tags: [String]
    public let source: [String]
    public let contentType: String  // "application/json"
}

// MARK: - PyJSON (ordered, sorted-key, ensure_ascii JSON encoder)

/// A minimal ordered JSON value model so we can serialize exactly the way
/// Python's `json.dumps(value, sort_keys=True)` does: ensure_ascii (every
/// non-ASCII codepoint -> \uXXXX with UTF-16 code units / surrogate pairs),
/// separators (", ", ": "), and keys sorted lexicographically by UTF-16 code
/// units. JSONEncoder.sortedKeys does NOT match (no ensure_ascii, no spaces),
/// so we hand-roll, mirroring pyjson.ts.
public indirect enum PyJSON {
    case null
    case bool(Bool)
    case int(Int)
    case string(String)
    case array([PyJSON])
    case object([(String, PyJSON)])
}

public enum PyJSONEncoder {
    /// Serialize like Python `json.dumps(value, sort_keys=True)` (default
    /// ensure_ascii, default separators). Mirrors pyjson.ts:pyJsonStringify.
    public static func stringify(_ value: PyJSON) -> String {
        encodeValue(value)
    }

    private static func encodeValue(_ v: PyJSON) -> String {
        switch v {
        case .null:
            return "null"
        case .bool(let b):
            return b ? "true" : "false"
        case .int(let i):
            // Integers print without a decimal point. Floats are not part of
            // the attention wire shape (duration_seconds is an int).
            return String(i)
        case .string(let s):
            return encodeString(s)
        case .array(let arr):
            return "[" + arr.map(encodeValue).joined(separator: ", ") + "]"
        case .object(let pairs):
            // sort_keys=True — sort by UTF-16 code units, matching JS's
            // Array.sort default (lexicographic by code unit) and Python's
            // str comparison for the BMP keys we emit.
            let sorted = pairs.sorted { lexLessUTF16($0.0, $1.0) }
            let parts = sorted.map { encodeString($0.0) + ": " + encodeValue($0.1) }
            return "{" + parts.join()
        }
    }

    /// Lexicographic comparison by UTF-16 code units (matches JS String < and
    /// pyjson.ts's Object.keys(obj).sort()).
    private static func lexLessUTF16(_ a: String, _ b: String) -> Bool {
        let au = Array(a.utf16)
        let bu = Array(b.utf16)
        var i = 0
        while i < au.count && i < bu.count {
            if au[i] != bu[i] { return au[i] < bu[i] }
            i += 1
        }
        return au.count < bu.count
    }

    /// Mirrors pyjson.ts:encodeString — ensure_ascii: control chars and every
    /// non-ASCII code unit become \uXXXX. Iterates UTF-16 code units so astral
    /// chars arrive as their surrogate pair, matching Python's ensure_ascii.
    private static func encodeString(_ s: String) -> String {
        var out = "\""
        for code in s.utf16 {
            switch code {
            case 0x22:  // "
                out += "\\\""
            case 0x5C:  // backslash
                out += "\\\\"
            case 0x08:  // \b
                out += "\\b"
            case 0x0C:  // \f
                out += "\\f"
            case 0x0A:  // \n
                out += "\\n"
            case 0x0D:  // \r
                out += "\\r"
            case 0x09:  // \t
                out += "\\t"
            default:
                if code < 0x20 || code > 0x7E {
                    out += "\\u" + String(format: "%04x", code)
                } else {
                    out += String(UnicodeScalar(code)!)
                }
            }
        }
        return out + "\""
    }
}

private extension Array where Element == String {
    /// Join the already-formatted object parts with ", " and close the brace.
    func join() -> String {
        joined(separator: ", ") + "}"
    }
}

// MARK: - ISO-8601 timestamp helpers

enum WireTime {
    /// Parse an ISO-8601 timestamp accepting a trailing 'Z' or an explicit
    /// numeric offset, optional fractional seconds. Returns whole epoch
    /// seconds, truncated toward negative infinity (floor), matching JS's
    /// `Math.floor(Date.parse(s) / 1000)`.
    static func epochSecondsFloor(_ s: String) -> Int? {
        guard let ms = parseISOMilliseconds(s) else { return nil }
        // floor division toward -inf (matches JS Math.floor of a real number).
        return Int(floor(Double(ms) / 1000.0))
    }

    /// Parse to integer epoch milliseconds, mirroring JS Date.parse for the
    /// shapes the attention payload uses (always an explicit zone).
    static func parseISOMilliseconds(_ s: String) -> Int? {
        // Hand-rolled to avoid DateFormatter/ISO8601DateFormatter rounding and
        // locale surprises, and to keep millisecond precision deterministic.
        // Grammar: YYYY-MM-DDTHH:MM:SS[.fff...][Z | (+|-)HH:MM]
        let chars = Array(s)
        var i = 0
        func readInt(_ n: Int) -> Int? {
            guard i + n <= chars.count else { return nil }
            var val = 0
            for k in 0..<n {
                guard let d = chars[i + k].wholeNumberValue, d >= 0, d <= 9 else { return nil }
                val = val * 10 + d
            }
            i += n
            return val
        }
        func expect(_ c: Character) -> Bool {
            guard i < chars.count, chars[i] == c else { return false }
            i += 1
            return true
        }
        guard let year = readInt(4), expect("-"),
              let month = readInt(2), expect("-"),
              let day = readInt(2),
              i < chars.count, (chars[i] == "T" || chars[i] == "t" || chars[i] == " ")
        else { return nil }
        i += 1  // consume 'T'
        guard let hour = readInt(2), expect(":"),
              let minute = readInt(2), expect(":"),
              let second = readInt(2)
        else { return nil }

        // Optional fractional seconds.
        var fractionalMs = 0
        if i < chars.count, chars[i] == "." {
            i += 1
            var digits = ""
            while i < chars.count, let d = chars[i].wholeNumberValue, d >= 0, d <= 9 {
                digits.append(chars[i])
                i += 1
            }
            // Take first 3 fractional digits as milliseconds (truncate, no
            // rounding) — JS Date.parse uses millisecond precision.
            let ms3 = String(digits.prefix(3))
            let padded = ms3.padding(toLength: 3, withPad: "0", startingAt: 0)
            fractionalMs = Int(padded) ?? 0
        }

        // Zone: 'Z'/'z' or (+|-)HH:MM (or (+|-)HHMM).
        var offsetMinutes = 0
        if i < chars.count {
            let z = chars[i]
            if z == "Z" || z == "z" {
                i += 1
            } else if z == "+" || z == "-" {
                let sign = (z == "-") ? -1 : 1
                i += 1
                guard let oh = readInt(2) else { return nil }
                var om = 0
                if i < chars.count, chars[i] == ":" {
                    i += 1
                    guard let m = readInt(2) else { return nil }
                    om = m
                } else if i < chars.count, chars[i].isNumber {
                    guard let m = readInt(2) else { return nil }
                    om = m
                }
                offsetMinutes = sign * (oh * 60 + om)
            } else {
                return nil
            }
        }

        // Days from civil (Howard Hinnant's algorithm) -> epoch days at UTC.
        let epochDays = daysFromCivil(year: year, month: month, day: day)
        let utcSeconds = epochDays * 86_400 + hour * 3_600 + minute * 60 + second
        let adjustedSeconds = utcSeconds - offsetMinutes * 60
        return adjustedSeconds * 1_000 + fractionalMs
    }

    /// Render whole epoch seconds as "YYYY-MM-DDTHH:MM:SS<suffix>" in UTC.
    /// Mirrors `new Date(sec*1000).toISOString().replace(".000Z", suffix)`.
    static func renderSecondISO(_ epochSeconds: Int, suffix: String) -> String {
        var days = epochSeconds / 86_400
        var rem = epochSeconds % 86_400
        if rem < 0 {  // floor toward -inf for negative epochs
            rem += 86_400
            days -= 1
        }
        let (y, m, d) = civilFromDays(days)
        let hh = rem / 3_600
        let mm = (rem % 3_600) / 60
        let ss = rem % 60
        return String(
            format: "%04d-%02d-%02dT%02d:%02d:%02d%@",
            y, m, d, hh, mm, ss, suffix
        )
    }

    // Howard Hinnant's days_from_civil / civil_from_days (proleptic Gregorian).
    private static func daysFromCivil(year: Int, month: Int, day: Int) -> Int {
        let y = (month <= 2) ? year - 1 : year
        let era = (y >= 0 ? y : y - 399) / 400
        let yoe = y - era * 400
        let doy = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
        return era * 146_097 + doe - 719_468
    }

    private static func civilFromDays(_ z0: Int) -> (Int, Int, Int) {
        let z = z0 + 719_468
        let era = (z >= 0 ? z : z - 146_096) / 146_097
        let doe = z - era * 146_097
        let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365
        let y = yoe + era * 400
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
        let mp = (5 * doy + 2) / 153
        let d = doy - (153 * mp + 2) / 5 + 1
        let m = mp + (mp < 10 ? 3 : -9)
        return (m <= 2 ? y + 1 : y, m, d)
    }
}

// MARK: - SHA-256 hex (CryptoKit)

enum WireHash {
    /// SHA-256 hex digest of a UTF-8 string. Mirrors sha256.ts:sha256Hex.
    static func sha256Hex(_ input: String) -> String {
        let digest = SHA256.hash(data: Data(input.utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }
}

// MARK: - URL scrubbing (port of scrub.ts)

enum WireScrub {
    /// Tier-1 denylist — must match scrub.ts:DENYLIST exactly.
    static let denylist: Set<String> = [
        // auth-bearing — OAuth 2 + plain
        "access_token", "id_token", "refresh_token", "code", "state", "nonce",
        "client_secret", "assertion", "session", "sid", "sessionid", "auth",
        "authorization", "token", "apikey", "api_key", "key", "signature",
        "sig", "hmac", "x-amz-signature", "x-amz-credential",
        "x-amz-security-token", "expires", "password", "pwd", "pw", "otp",
        "magic", "share_token", "invite", "confirmation_token",
        "_csrf", "csrf_token", "xsrf", "ticket", "ott",
        // OAuth 1.0a
        "oauth_token", "oauth_verifier", "oauth_signature", "oauth_callback",
        "oauth_consumer_key", "oauth_nonce", "oauth_timestamp",
        // tracking
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "gclid", "fbclid", "msclkid", "mc_eid", "mc_cid", "_hsenc", "_hsmi",
        "igshid", "yclid", "ref", "ref_src", "ref_url",
        // one-click action
        "unsubscribe", "unsub", "verify", "reset", "confirm", "activate",
        // magic-link identifiers
        "email", "username", "login", "magic_link",
    ]

    /// Port of scrub.ts:scrubUrl. Drops denylisted query params (preserving
    /// the order of survivors) and the fragment entirely.
    static func scrubURL(_ input: String) -> String {
        guard let comps = URLComponents(string: input) else { return input }
        var out = comps
        out.fragment = nil
        if let items = comps.queryItems {
            let kept = items.filter { !denylist.contains($0.name.lowercased()) }
            out.queryItems = kept.isEmpty ? nil : kept
        }
        // Rebuild base "scheme://host[:port]/path" and append the kept query.
        let scheme = comps.scheme ?? ""
        var host = comps.host ?? ""
        if let port = comps.port { host += ":\(port)" }
        let base = "\(scheme)://\(host)\(comps.path)"
        // out.percentEncodedQuery preserves the surviving params' raw encoding.
        if let q = out.percentEncodedQuery, !q.isEmpty {
            return "\(base)?\(q)"
        }
        return base
    }

    /// Lowercased hostname of a URL, matching scrub/host derivation. Returns
    /// nil for a URL without a host. Mirrors wire.ts:hostnameOf.
    static func hostname(_ url: String) -> String? {
        guard let comps = URLComponents(string: url), let h = comps.host, !h.isEmpty else {
            return nil
        }
        return h.lowercased()
    }
}

// MARK: - sourceId

public extension Wire {
    /// Deterministic source-id from `key` (scrubbed URL or category), the
    /// second-truncated start time, and the per-browser identity slug:
    ///   sec = start_time truncated to seconds, ISO with "+00:00" offset
    ///   sha256("\(key)|\(sec)|\(identitySlug)")[:16]
    /// Mirrors wire.ts:sourceId. Returns nil only if the timestamp is
    /// unparseable.
    static func sourceId(
        key: String,
        startTimeISO: String,
        identitySlug: String
    ) -> String? {
        guard let sec = WireTime.epochSecondsFloor(startTimeISO) else { return nil }
        // Tz-aware UTC second-form -> "YYYY-MM-DDTHH:MM:SS+00:00".
        let secISO = WireTime.renderSecondISO(sec, suffix: "+00:00")
        let hash = WireHash.sha256Hex("\(key)|\(secISO)|\(identitySlug)")
        return sourcePrefix + String(hash.prefix(16))
    }
}

// MARK: - buildWireRecord

public extension Wire {
    enum WireError: Error {
        case unparseableTimestamp(String)
    }

    /// Transform a single AttentionEvent into its wire record + source_id.
    /// Mirrors wire.ts:buildWireRecord. Assumes the payload is already
    /// validated (exactly one of url/category non-null).
    static func buildWireRecord(
        event: AttentionEvent,
        context ctx: WireContext
    ) throws -> WireResult {
        let url: String? = event.url != nil ? WireScrub.scrubURL(event.url!) : nil
        let category = event.category
        let title = event.title

        let host: String?
        let note: String
        let sidKey: String
        if let url = url {
            host = WireScrub.hostname(url)
            note = (title != nil && !title!.isEmpty) ? "\(title!) — \(url)" : url
            sidKey = url
        } else {
            host = nil
            note = "Attention: \(category ?? "")"
            sidKey = category ?? ""
        }

        guard let startSec = WireTime.epochSecondsFloor(event.startTime) else {
            throw WireError.unparseableTimestamp(event.startTime)
        }
        guard let endSec = WireTime.epochSecondsFloor(event.endTime) else {
            throw WireError.unparseableTimestamp(event.endTime)
        }
        let startSecISO = WireTime.renderSecondISO(startSec, suffix: "Z")
        let endSecISO = WireTime.renderSecondISO(endSec, suffix: "Z")
        let durationSeconds = max(0, endSec - startSec)

        guard let sid = sourceId(
            key: sidKey,
            startTimeISO: event.startTime,
            identitySlug: ctx.identitySlug
        ) else {
            throw WireError.unparseableTimestamp(event.startTime)
        }

        // external_ids — key order irrelevant: PyJSONEncoder sorts.
        let externalIds = PyJSON.object([
            ("client", .string(event.client)),
            ("host", host.map { PyJSON.string($0) } ?? .null),
            ("chrome_identity", event.chromeIdentity.map { PyJSON.string($0) } ?? .null),
            ("og_type", event.ogType.map { PyJSON.string($0) } ?? .null),
            ("lang", event.lang.map { PyJSON.string($0) } ?? .null),
            ("device", ctx.identitySlug.isEmpty ? .null : .string(ctx.identitySlug)),
            ("device_label", ctx.identityLabel.map { PyJSON.string($0) } ?? .null),
        ])

        let dataInner = PyJSON.object([
            ("note", .string(note)),
            ("title", title.map { PyJSON.string($0) } ?? .null),
            ("service", .string("web")),
            ("category", category.map { PyJSON.string($0) } ?? .null),
            ("url", url.map { PyJSON.string($0) } ?? .null),
            ("og_description", event.ogDescription.map { PyJSON.string($0) } ?? .null),
            ("favicon_url", event.faviconURL.map { PyJSON.string($0) } ?? .null),
            ("parent_source_id", .null),
            ("duration_seconds", .int(durationSeconds)),
            ("external_ids", externalIds),
        ])

        let source = [sid, "com.fulcradynamics.annotation.\(ctx.definitionId)"]

        let record = WireRecord(
            specversion: 1,
            data: PyJSONEncoder.stringify(dataInner),
            dataType: dataType,
            recordedAtStart: startSecISO,
            recordedAtEnd: endSecISO,
            tags: ctx.tagIds,
            source: source,
            contentType: "application/json"
        )

        return WireResult(record: record, sourceId: sid)
    }

    /// Encode records as the JSONL body for POST /ingest/v1/record/batch — one
    /// sorted-key JSON object per line, newline-joined. Mirrors
    /// wire.ts:encodeBatch.
    static func encodeBatch(_ records: [WireRecord]) -> String {
        records.map { record -> String in
            PyJSONEncoder.stringify(.object([
                ("specversion", .int(record.specversion)),
                ("data", .string(record.data)),
                ("metadata", .object([
                    ("data_type", .string(record.dataType)),
                    ("recorded_at", .object([
                        ("start_time", .string(record.recordedAtStart)),
                        ("end_time", .string(record.recordedAtEnd)),
                    ])),
                    ("tags", .array(record.tags.map { PyJSON.string($0) })),
                    ("source", .array(record.source.map { PyJSON.string($0) })),
                    ("content_type", .string(record.contentType)),
                ])),
            ]))
        }.joined(separator: "\n")
    }
}
