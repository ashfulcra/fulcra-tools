//
//  EnsureDefinition.swift
//  FulcraAttention (macOS App)
//
//  Native Swift port of chrome/src/relayless/ensureDefinition.ts — the
//  relayless "ensure the Attention definition + tags exist" job. There is no
//  localhost daemon on this path, so the native app resolves/creates the Fulcra
//  "Attention" duration-annotation definition + its canonical tags directly
//  against the Fulcra Data API, and hands the resolved {definitionId, tagIds}
//  to the native ingest layer (the same two things the wire transform needs).
//
//  IDEMPOTENT find-or-create, behavior-compatible with the TypeScript source of
//  truth:
//    - tag find/create: GET /user/v1alpha1/tag/name/{name} (200 → {id}) else
//      POST /user/v1alpha1/tag {name} (→ {id}).
//    - definition adopt-by-name: list GET /user/v1alpha1/annotation, keep
//      name=="Attention" && annotation_type=="duration" && !deleted_at &&
//      id is a string, sort by created_at oldest-first, take the oldest (so
//      every machine converges on one def). Else POST the canonical create body.
//
//  The resolved {definitionId, tagIds} is cached (UserDefaults, NOT the
//  Keychain — these are non-secret ids) so resolution happens ONCE.
//
//  Source of truth: packages/attention/chrome/src/relayless/ensureDefinition.ts
//

import Foundation
import os

// MARK: - Logging

/// Structured logging (per project CLAUDE.md: levels + component/operation
/// context). Never logs tokens or secrets.
private nonisolated let ensureLog = Logger(
    subsystem: "com.fulcra.attention",
    category: "ensure-definition"
)

// MARK: - Canonical Attention descriptor (mirrors definition_spec.py / TS)

/// ATTENTION_CANONICAL["name"].
public nonisolated let ATTENTION_DEFINITION_NAME = "Attention"
/// ATTENTION_CANONICAL["description"].
public nonisolated let ATTENTION_DEFINITION_DESCRIPTION =
    "What the user paid attention to (browsing)."
/// ATTENTION_DEFINITION_TAG_NAMES — the tags the def is created with, in order
/// (attention, web) as the leading tagIds.
public nonisolated let ATTENTION_DEFINITION_TAG_NAMES = ["attention", "web"]

/// The prefix on every per-browser machine tag.
public nonisolated let MACHINE_TAG_PREFIX = "machine:"
/// The Fulcra API caps tag names at 30 chars (HTTP 422 otherwise).
public nonisolated let MAX_TAG_NAME_LEN = 30
/// The slug budget so MACHINE_TAG_PREFIX + slug stays within MAX_TAG_NAME_LEN
/// (= 22).
public nonisolated let MACHINE_SLUG_BUDGET = MAX_TAG_NAME_LEN - MACHINE_TAG_PREFIX.count

/// The Fulcra Data API base. Mirrors chrome/src/relayless/config.ts API_BASE.
public nonisolated let FULCRA_API_BASE = "https://api.fulcradynamics.com"

// MARK: - slugify (BYTE-PARITY CRITICAL with JS)

/// Slugify a per-browser identity label into a tag-safe token: lowercase,
/// collapse any run of characters outside [a-z0-9] into a single "-", trim
/// leading/trailing "-", then TRUNCATE to MACHINE_SLUG_BUDGET chars and re-trim
/// any trailing "-" left by the cut. Empty/all-separator → "browser".
///
/// This MUST match JS:
///   label.toLowerCase()
///     .replace(/[^a-z0-9]+/g, "-")
///     .replace(/^-+|-+$/g, "")
///     .slice(0, 22)
///     .replace(/-+$/g, "")
///
/// JS regex `[^a-z0-9]` treats *every* codepoint that is not ASCII a-z/0-9 as a
/// separator (multi-byte chars included). We deliberately do a manual scan over
/// the lowercased string's UTF-16 code units rather than a Unicode-aware regex,
/// so that semantics match JS exactly:
///   - JS `.toLowerCase()` and Swift `.lowercased()` agree on ASCII; for the
///     classification step we only care whether a unit is in ASCII [a-z0-9].
///   - `.slice(0, 22)` and `String.prototype` length operate on UTF-16 code
///     units in JS, so we truncate by UTF-16 units too (matters only for
///     astral/multi-byte input, which always becomes separators here anyway).
public func slugifyIdentity(_ label: String) -> String {
    let lowered = label.lowercased()

    // Step 1+2: lowercase already done; replace runs of non-[a-z0-9] with "-"
    // and trim leading/trailing "-". Operate over UTF-16 code units to match
    // JS string semantics (length/slice are UTF-16-based in JS).
    var collapsed: [UInt16] = []
    var pendingSeparator = false
    var emittedAny = false
    let dash: UInt16 = 0x2D // '-'
    for unit in lowered.utf16 {
        let isAllowed =
            (unit >= 0x61 && unit <= 0x7A) || // a-z
            (unit >= 0x30 && unit <= 0x39)    // 0-9
        if isAllowed {
            if pendingSeparator && emittedAny {
                collapsed.append(dash)
            }
            pendingSeparator = false
            collapsed.append(unit)
            emittedAny = true
        } else {
            // Any non-[a-z0-9] unit is a separator (JS [^a-z0-9] semantics).
            if emittedAny {
                pendingSeparator = true
            }
            // Leading separators are dropped (emittedAny stays false), matching
            // the trim of `^-+`.
        }
    }
    // Trailing separators dropped by virtue of only emitting a dash before the
    // next allowed char (matches trim of `-+$`).

    // Step 3: slice(0, 22) over UTF-16 units.
    if collapsed.count > MACHINE_SLUG_BUDGET {
        collapsed = Array(collapsed.prefix(MACHINE_SLUG_BUDGET))
    }

    // Step 4: re-trim any trailing "-" left by the cut.
    while let last = collapsed.last, last == dash {
        collapsed.removeLast()
    }

    let slug = String(utf16CodeUnits: collapsed, count: collapsed.count)
    return slug.isEmpty ? "browser" : slug
}

/// The per-browser machine tag name for an identity label: "machine:<slug>".
/// Always ≤ MAX_TAG_NAME_LEN (30) chars.
public func machineTagName(_ label: String) -> String {
    return "\(MACHINE_TAG_PREFIX)\(slugifyIdentity(label))"
}

// MARK: - Percent-encoding (JS encodeURIComponent parity)

/// Mirror JS `encodeURIComponent(name)`: encode everything that is not an
/// "unreserved" character. JS leaves only `A-Z a-z 0-9 - _ . ! ~ * ' ( )`
/// unescaped; everything else (including ":" → %3A and " " → %20) is encoded.
func encodeURIComponentJS(_ s: String) -> String {
    var allowed = CharacterSet()
    allowed.insert(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    allowed.insert(charactersIn: "abcdefghijklmnopqrstuvwxyz")
    allowed.insert(charactersIn: "0123456789")
    allowed.insert(charactersIn: "-_.!~*'()")
    // addingPercentEncoding encodes anything not in `allowed`. Force-unwrap is
    // safe: it only returns nil for invalid Unicode, which String cannot hold.
    return s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s
}

// MARK: - Result + opts

/// The resolved {definitionId, tagIds} for the relayless Attention transport.
public struct ResolvedAttention: Codable, Equatable, Sendable {
    /// The Attention annotation-definition id.
    public let definitionId: String
    /// Resolved tag ids for ATTENTION_DEFINITION_TAG_NAMES, in order
    /// [attention, web] (+ optional trailing machine:<slug>).
    public let tagIds: [String]

    public init(definitionId: String, tagIds: [String]) {
        self.definitionId = definitionId
        self.tagIds = tagIds
    }
}

/// A user-selectable Attention destination definition (for onboarding).
public struct AttentionDestination: Equatable, Sendable {
    /// The annotation-definition id.
    public let id: String
    /// The definition name (always "Attention" — that's the filter predicate).
    public let name: String
    /// ISO created_at, or nil when the API omitted it.
    public let createdAt: String?
    /// True for the one `ensureAttentionDefinitionAndTags` would auto-adopt
    /// (the oldest live def — index 0 after the oldest-first sort).
    public let isAutoPick: Bool

    public init(id: String, name: String, createdAt: String?, isAutoPick: Bool) {
        self.id = id
        self.name = name
        self.createdAt = createdAt
        self.isAutoPick = isAutoPick
    }
}

// MARK: - Errors

/// Thrown when the API rejected the token (401). The ingest/onboarding layer
/// maps this to the "needs sign-in" error state. Distinct type so callers can
/// catch it specifically.
public struct UnauthorizedError: Error, LocalizedError, CustomStringConvertible {
    public let message: String
    public init(_ message: String = "unauthorized") { self.message = message }
    public var errorDescription: String? { message }
    public var description: String { message }
}

/// Generic non-2xx (non-401) API/transport failure. Mirrors the TS generic
/// `Error("\(what): HTTP \(status)")`.
public struct EnsureDefinitionError: Error, LocalizedError, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
    public var description: String { message }
}

// MARK: - Injectable token provider (mirrors TS getToken({force?}))

/// Mirrors the TS `getToken: (opts?: { force?: boolean }) => Promise<string|null>`
/// contract. A normal call returns the current access token; a `forceRefresh:
/// true` call refreshes first.
public protocol TokenProvider: Sendable {
    /// Return a valid Bearer access token, or nil when not signed in.
    /// `forceRefresh == true` forces a token refresh before returning.
    func accessToken(forceRefresh: Bool) async throws -> String?
}

// MARK: - Injectable HTTP transport

/// A minimal async HTTP transport. The default is `URLSession`-backed; tests
/// supply a URLProtocol-stubbed `URLSession` (so the real URLSession code path
/// is exercised) or a fake conforming to this protocol.
public protocol HTTPClient: Sendable {
    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

/// Default `URLSession`-backed transport.
public struct URLSessionHTTPClient: HTTPClient {
    private let session: URLSession
    public init(session: URLSession = .shared) { self.session = session }

    public func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw EnsureDefinitionError("non-HTTP response from \(request.url?.absoluteString ?? "?")")
        }
        return (data, http)
    }
}

// MARK: - Injectable resolved-id cache (mirrors TS StorageArea cache)

/// Persistence for the resolved {definitionId, tagIds}. Non-secret ids only —
/// the default backing is UserDefaults, NOT the Keychain.
public protocol ResolvedAttentionCache: Sendable {
    func read() -> ResolvedAttention?
    func write(_ resolved: ResolvedAttention)
    func clear()
}

/// UserDefaults-backed cache. Defaults to `.standard`; pass a suite when
/// app/extension sharing is wired up. `UserDefaults` is itself thread-safe, so
/// the `@unchecked Sendable` conformance is sound (mirrors AuthManager's
/// posture for its non-Sendable members).
public final class UserDefaultsResolvedCache: ResolvedAttentionCache, @unchecked Sendable {
    private let defaults: UserDefaults
    private let key: String

    public static let defaultKey = "relaylessResolvedAttention"

    public init(defaults: UserDefaults = .standard, key: String = UserDefaultsResolvedCache.defaultKey) {
        self.defaults = defaults
        self.key = key
    }

    public func read() -> ResolvedAttention? {
        guard let data = defaults.data(forKey: key) else { return nil }
        guard let v = try? JSONDecoder().decode(ResolvedAttention.self, from: data) else {
            return nil
        }
        // Mirror the TS validity guard: definitionId is a string, tagIds is an
        // array. Decoding already enforces the types; an empty definitionId is
        // still "valid" per TS (it checks typeof === "string", not non-empty).
        return v
    }

    public func write(_ resolved: ResolvedAttention) {
        guard let data = try? JSONEncoder().encode(resolved) else { return }
        defaults.set(data, forKey: key)
    }

    public func clear() {
        defaults.removeObject(forKey: key)
    }
}

// MARK: - EnsureAttention (the ported module)

/// The relayless Attention resolver. One instance bundles the token provider,
/// HTTP transport, and resolved-id cache. Methods mirror the TS module's
/// exported functions 1:1.
public final class EnsureAttention: @unchecked Sendable {
    private let token: TokenProvider
    private let http: HTTPClient
    private let cache: ResolvedAttentionCache
    private let apiBase: String

    public init(
        token: TokenProvider,
        http: HTTPClient = URLSessionHTTPClient(),
        cache: ResolvedAttentionCache = UserDefaultsResolvedCache(),
        apiBase: String = FULCRA_API_BASE
    ) {
        self.token = token
        self.http = http
        self.cache = cache
        self.apiBase = apiBase
    }

    // MARK: Public API (mirrors the TS exported functions)

    /// Resolve {definitionId, tagIds}, find-or-creating the def + tags.
    /// Idempotent: an existing "Attention" def is adopted (never duplicated),
    /// existing tags are found (never re-created). Cache hit → no network.
    /// Throws UnauthorizedError on 401 / no token.
    public func ensureAttentionDefinitionAndTags(
        identityLabel: String? = nil
    ) async throws -> ResolvedAttention {
        ensureLog.debug("ensure: start (identityLabel set: \(identityLabel?.isEmpty == false))")
        if let cached = cache.read() {
            ensureLog.info("ensure: cache hit; no network")
            return cached
        }
        ensureLog.debug("ensure: cache miss; resolving")
        var ctx = try await makeCtx()

        let tagIds = try await resolveAttentionTagIds(&ctx, identityLabel: identityLabel)

        var definitionId = try await findAttentionDefinition(&ctx)
        if definitionId == nil {
            ensureLog.info("ensure: no existing def; creating")
            definitionId = try await createAttentionDefinition(&ctx, tagIds: tagIds, name: ATTENTION_DEFINITION_NAME)
        } else {
            ensureLog.info("ensure: adopted existing def")
        }

        let resolved = ResolvedAttention(definitionId: definitionId!, tagIds: tagIds)
        cache.write(resolved)
        ensureLog.info("ensure: resolved and cached")
        return resolved
    }

    /// List the live "Attention" duration definitions, oldest-first. Index 0 is
    /// `isAutoPick: true`. Empty list → none exist yet. One GET; no cache touch.
    public func listAttentionDestinations() async throws -> [AttentionDestination] {
        ensureLog.debug("listDestinations: start")
        var ctx = try await makeCtx()
        let matches = try await findAttentionDefinitionRows(&ctx)
        return matches.enumerated().map { (i, d) in
            AttentionDestination(
                id: d.id ?? "",
                name: ATTENTION_DEFINITION_NAME,
                createdAt: d.created_at,
                isAutoPick: i == 0
            )
        }
    }

    /// Adopt an EXISTING def by id: resolve tags, write the cache, return.
    /// Never creates a def.
    public func chooseAttentionDestination(
        definitionId: String,
        identityLabel: String? = nil
    ) async throws -> ResolvedAttention {
        ensureLog.debug("chooseDestination: \(definitionId, privacy: .public)")
        var ctx = try await makeCtx()
        let tagIds = try await resolveAttentionTagIds(&ctx, identityLabel: identityLabel)
        let resolved = ResolvedAttention(definitionId: definitionId, tagIds: tagIds)
        cache.write(resolved)
        return resolved
    }

    /// Create a FRESH def (optionally named) and adopt it: resolve tags, POST
    /// the canonical create body, write the cache, return. Defaults name to
    /// "Attention".
    public func createAttentionDestination(
        name: String = ATTENTION_DEFINITION_NAME,
        identityLabel: String? = nil
    ) async throws -> ResolvedAttention {
        ensureLog.debug("createDestination: name=\(name, privacy: .public)")
        var ctx = try await makeCtx()
        let tagIds = try await resolveAttentionTagIds(&ctx, identityLabel: identityLabel)
        let definitionId = try await createAttentionDefinition(&ctx, tagIds: tagIds, name: name)
        let resolved = ResolvedAttention(definitionId: definitionId, tagIds: tagIds)
        cache.write(resolved)
        return resolved
    }

    /// Re-resolve + rewrite cached tagIds for a NEW identity label, keeping the
    /// existing definitionId. No cache → no-op, returns nil.
    public func updateIdentity(label: String?) async throws -> ResolvedAttention? {
        guard let cached = cache.read() else {
            ensureLog.debug("updateIdentity: no cache; no-op")
            return nil
        }
        var ctx = try await makeCtx()
        let tagIds = try await resolveAttentionTagIds(&ctx, identityLabel: label)
        let resolved = ResolvedAttention(definitionId: cached.definitionId, tagIds: tagIds)
        cache.write(resolved)
        return resolved
    }

    /// Clear the cached resolution (e.g. on sign-out / account switch).
    public func clearResolvedAttention() {
        ensureLog.info("clearResolvedAttention")
        cache.clear()
    }

    // MARK: - internals

    /// Per-request auth context: the current token + a force-refresh hook.
    /// `token` is mutated in place by `authedFetch` after a successful forced
    /// refresh so subsequent requests reuse the fresh token (mirrors the TS
    /// ApiCtx whose token field is swapped).
    private struct ApiCtx {
        var token: String
    }

    /// Build an authenticated ApiCtx, requiring a token. Throws
    /// UnauthorizedError when not signed in — BEFORE any network call.
    private func makeCtx() async throws -> ApiCtx {
        guard let tok = try await token.accessToken(forceRefresh: false), !tok.isEmpty else {
            ensureLog.error("not signed in (no token); throwing UnauthorizedError before any fetch")
            throw UnauthorizedError("not signed in")
        }
        return ApiCtx(token: tok)
    }

    /// Authenticated fetch with a single force-refresh-retry on 401. Injects
    /// `Authorization: Bearer <ctx.token>`. On 401: call
    /// token.accessToken(forceRefresh: true); if nil → return the 401 response
    /// (caller's assertOk raises UnauthorizedError); else swap the token into
    /// ctx and retry ONCE.
    private func authedFetch(
        _ ctx: inout ApiCtx,
        url: String,
        method: String,
        contentType: String? = nil,
        body: Data? = nil
    ) async throws -> (Data, HTTPURLResponse) {
        func makeRequest(token: String) -> URLRequest {
            var req = URLRequest(url: URL(string: url)!)
            req.httpMethod = method
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            if let contentType { req.setValue(contentType, forHTTPHeaderField: "Content-Type") }
            if let body { req.httpBody = body }
            return req
        }

        var (data, resp) = try await http.send(makeRequest(token: ctx.token))
        if resp.statusCode != 401 { return (data, resp) }

        // Server rejected the (locally-fresh) token. Force a refresh, retry once.
        ensureLog.info("authedFetch: 401; attempting one forced token refresh + retry")
        guard let fresh = try await token.accessToken(forceRefresh: true), !fresh.isEmpty else {
            ensureLog.error("authedFetch: forced refresh yielded no token; surfacing 401")
            return (data, resp) // refresh failed → keep the 401 for assertOk.
        }
        ctx.token = fresh
        (data, resp) = try await http.send(makeRequest(token: fresh))
        return (data, resp)
    }

    /// Resolve canonical tag ids in ATTENTION_DEFINITION_TAG_NAMES order, then
    /// — when identityLabel is non-nil and trimmed non-empty — append the
    /// per-browser machine tag.
    private func resolveAttentionTagIds(
        _ ctx: inout ApiCtx,
        identityLabel: String?
    ) async throws -> [String] {
        var tagIds: [String] = []
        for name in ATTENTION_DEFINITION_TAG_NAMES {
            tagIds.append(try await resolveTag(&ctx, name: name))
        }
        if let label = identityLabel,
           !label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            tagIds.append(try await resolveTag(&ctx, name: machineTagName(label)))
        }
        return tagIds
    }

    /// Raise UnauthorizedError on 401, generic EnsureDefinitionError on any
    /// other non-2xx.
    private func assertOk(_ resp: HTTPURLResponse, _ what: String) throws {
        if resp.statusCode == 401 { throw UnauthorizedError("\(what): 401") }
        if resp.statusCode < 200 || resp.statusCode >= 300 {
            throw EnsureDefinitionError("\(what): HTTP \(resp.statusCode)")
        }
    }

    /// Tag find-or-create: GET /user/v1alpha1/tag/name/{name} → 200 {id}; else
    /// POST /user/v1alpha1/tag {name} → {id}. The name is percent-encoded in the
    /// GET path (encodeURIComponent semantics); the POST body carries the raw
    /// name.
    private func resolveTag(_ ctx: inout ApiCtx, name: String) async throws -> String {
        let path = encodeURIComponentJS(name)
        let (getData, getResp) = try await authedFetch(
            &ctx,
            url: "\(apiBase)/user/v1alpha1/tag/name/\(path)",
            method: "GET"
        )
        if getResp.statusCode == 200 {
            if let id = decodeId(getData) {
                ensureLog.debug("resolveTag: GET found \(name, privacy: .public)")
                return id
            }
            throw EnsureDefinitionError("tag lookup for \(name): missing id in 200 body")
        }
        if getResp.statusCode == 401 { throw UnauthorizedError("tag lookup: 401") }

        // Any non-200, non-401 (typically 404) → create it.
        ensureLog.debug("resolveTag: POST create \(name, privacy: .public)")
        let bodyData = try JSONSerialization.data(withJSONObject: ["name": name], options: [])
        let (postData, postResp) = try await authedFetch(
            &ctx,
            url: "\(apiBase)/user/v1alpha1/tag",
            method: "POST",
            contentType: "application/json",
            body: bodyData
        )
        try assertOk(postResp, "tag create for \(name)")
        guard let id = decodeId(postData) else {
            throw EnsureDefinitionError("tag create for \(name): missing id in response")
        }
        return id
    }

    /// Return the id of the live "Attention" duration definition, or nil
    /// (oldest-first → index 0).
    private func findAttentionDefinition(_ ctx: inout ApiCtx) async throws -> String? {
        let matches = try await findAttentionDefinitionRows(&ctx)
        return matches.first?.id
    }

    /// GET the annotation list and return live "Attention" duration rows,
    /// sorted oldest-first by created_at.
    private func findAttentionDefinitionRows(_ ctx: inout ApiCtx) async throws -> [DefinitionRow] {
        let (data, resp) = try await authedFetch(
            &ctx,
            url: "\(apiBase)/user/v1alpha1/annotation",
            method: "GET"
        )
        try assertOk(resp, "list annotation definitions")

        let rows = parseDefinitionRows(data)
        var matches = rows.filter { d in
            d.name == ATTENTION_DEFINITION_NAME &&
            d.annotation_type == "duration" &&
            !(d.deleted_at != nil && !(d.deleted_at!.isEmpty)) && // !deleted_at (null/absent/"" are falsy)
            d.id != nil
        }
        // Sort by created_at ascending with Foundation localized comparison,
        // matching the TS `(a.created_at || "").localeCompare(b.created_at ||
        // "")` better than raw Swift `<` for punctuation and non-ASCII edge
        // cases. Keep a stable tie-break because JS Array.sort is stable in
        // modern V8/JSC.
        matches = matches.enumerated().sorted { lhs, rhs in
            let a = lhs.element.created_at ?? ""
            let b = rhs.element.created_at ?? ""
            if a == b { return lhs.offset < rhs.offset }
            return a.localizedCompare(b) == .orderedAscending
        }.map { $0.element }
        return matches
    }

    /// Create the canonical Attention definition (optionally named). POST the
    /// full create payload with the resolved tag ids attached.
    private func createAttentionDefinition(
        _ ctx: inout ApiCtx,
        tagIds: [String],
        name: String
    ) async throws -> String {
        let payload = attentionCreatePayload(tagIds: tagIds, name: name)
        let bodyData = try JSONSerialization.data(withJSONObject: payload, options: [])
        let (data, resp) = try await authedFetch(
            &ctx,
            url: "\(apiBase)/user/v1alpha1/annotation",
            method: "POST",
            contentType: "application/json",
            body: bodyData
        )
        try assertOk(resp, "create annotation definition")
        guard let id = decodeId(data) else {
            throw EnsureDefinitionError("create annotation definition: missing id in response")
        }
        return id
    }

    // MARK: JSON helpers

    /// A minimal annotation-definition row (only the fields we read).
    private struct DefinitionRow {
        var id: String?
        var name: String?
        var annotation_type: String?
        var deleted_at: String?
        var created_at: String?
    }

    /// Decode `{id}` from a response body, returning the id only when it is a
    /// JSON string (mirrors `typeof body.id === "string"`).
    private func decodeId(_ data: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return obj["id"] as? String
    }

    /// Parse the annotation-list response into DefinitionRows. A non-array body
    /// yields []. Only reads the fields we care about; coerces id to a string
    /// only when it actually is one (mirrors `typeof d.id === "string"` filter).
    private func parseDefinitionRows(_ data: Data) -> [DefinitionRow] {
        guard let arr = try? JSONSerialization.jsonObject(with: data) as? [Any] else {
            return []
        }
        return arr.compactMap { element in
            guard let d = element as? [String: Any] else { return nil }
            return DefinitionRow(
                id: d["id"] as? String,
                name: d["name"] as? String,
                annotation_type: d["annotation_type"] as? String,
                deleted_at: d["deleted_at"] as? String,
                created_at: d["created_at"] as? String
            )
        }
    }

    /// The FULL create body for the Attention duration definition. Mirrors
    /// attentionCreatePayload in the TS: annotation_type "duration",
    /// measurement_spec carrying measurement_type/value_type "duration", unit
    /// null. Order of keys is irrelevant to the API (JSON object), but the test
    /// compares the decoded object so any order is fine.
    private func attentionCreatePayload(tagIds: [String], name: String) -> [String: Any] {
        return [
            "annotation_type": "duration",
            "name": name,
            "description": ATTENTION_DEFINITION_DESCRIPTION,
            "tags": tagIds,
            "measurement_spec": [
                "measurement_type": "duration",
                "value_type": "duration",
                "unit": NSNull(),
            ] as [String: Any],
        ]
    }
}
