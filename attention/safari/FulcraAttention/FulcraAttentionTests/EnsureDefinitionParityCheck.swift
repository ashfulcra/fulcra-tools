//
//  EnsureDefinitionParityCheck.swift
//  FulcraAttention — standalone parity / behavioral check.
//
//  The xcodeproj has no XCTest target, so — following the WireParityCheck
//  pattern referenced by the controller — this is a standalone `@main` script
//  that compiles together with the module source via `swiftc` and exercises:
//    1. slugify golden cases (BYTE-PARITY with the TS source of truth), incl. a
//       multi-byte char case verified against JS [^a-z0-9] semantics.
//    2. the full behavioral contract from
//       chrome/tests/relayless/ensureDefinition.test.ts, using in-memory fakes
//       for the token provider, HTTP transport, and resolved-id cache.
//
//  Run:
//    swiftc -parse-as-library \
//      "macOS (App)/EnsureDefinition.swift" \
//      FulcraAttentionTests/EnsureDefinitionParityCheck.swift \
//      -o /tmp/ensure-parity && /tmp/ensure-parity
//
//  Exit code 0 = all checks passed; non-zero = a check failed (with detail).
//

import Foundation

// MARK: - Tiny assertion harness

final class Checks {
    var passed = 0
    var failed = 0
    var failures: [String] = []

    func ok(_ cond: Bool, _ what: String) {
        if cond { passed += 1 }
        else { failed += 1; failures.append("FAIL: \(what)") }
    }

    func eq<T: Equatable>(_ a: T, _ b: T, _ what: String) {
        if a == b { passed += 1 }
        else { failed += 1; failures.append("FAIL: \(what) — expected \(b), got \(a)") }
    }

    func report() -> Int32 {
        for f in failures { FileHandle.standardError.write((f + "\n").data(using: .utf8)!) }
        print("\n\(passed) passed, \(failed) failed")
        return failed == 0 ? 0 : 1
    }
}

// MARK: - Fakes

/// Token provider returning a fixed token; or, when `forcedToken` is set, a
/// different token on forceRefresh (to drive the 401-retry path).
final class FakeToken: TokenProvider, @unchecked Sendable {
    let normal: String?
    let forced: String?
    var forceCalledWith: [Bool] = []
    init(normal: String?, forced: String? = nil) { self.normal = normal; self.forced = forced }
    func accessToken(forceRefresh: Bool) async throws -> String? {
        forceCalledWith.append(forceRefresh)
        return forceRefresh ? (forced ?? normal) : normal
    }
}

/// In-memory cache fake (mirrors the TS memStorage).
final class FakeCache: ResolvedAttentionCache, @unchecked Sendable {
    private var value: ResolvedAttention?
    func read() -> ResolvedAttention? { value }
    func write(_ resolved: ResolvedAttention) { value = resolved }
    func clear() { value = nil }
}

/// A scriptable HTTP fake mirroring the TS `makeApi`. Records calls and routes
/// the Data API endpoints. `tags` maps tag name -> id (nil = 404 → create).
final class FakeApi: HTTPClient, @unchecked Sendable {
    var tags: [String: String?]
    var defs: [[String: Any]]
    var defCreateId: String
    /// When set, every request 401s unless Authorization == "Bearer <gateToken>".
    var gateToken: String?

    // recorded
    var tagGets: [String] = []
    var tagPosts: [String] = []
    var defGets = 0
    var defPosts: [[String: Any]] = []
    var totalCalls = 0
    var seenAuth: [String] = []

    private var tagCreateSeq = 1000

    init(tags: [String: String?] = [:], defs: [[String: Any]] = [], defCreateId: String = "new-def", gateToken: String? = nil) {
        self.tags = tags; self.defs = defs; self.defCreateId = defCreateId; self.gateToken = gateToken
    }

    private func json(_ obj: Any, status: Int, url: URL) -> (Data, HTTPURLResponse) {
        let data = try! JSONSerialization.data(withJSONObject: obj, options: [])
        let resp = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!
        return (data, resp)
    }
    private func raw(_ s: String, status: Int, url: URL) -> (Data, HTTPURLResponse) {
        let resp = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!
        return (s.data(using: .utf8)!, resp)
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        totalCalls += 1
        let auth = request.value(forHTTPHeaderField: "Authorization") ?? ""
        seenAuth.append(auth)
        let url = request.url!
        let urlStr = url.absoluteString
        let method = (request.httpMethod ?? "GET").uppercased()

        if let gate = gateToken, auth != "Bearer \(gate)" {
            return raw("", status: 401, url: url)
        }

        if urlStr.contains("/user/v1alpha1/tag/name/") {
            let encoded = urlStr.components(separatedBy: "/tag/name/")[1]
            let name = encoded.removingPercentEncoding ?? encoded
            tagGets.append(name)
            if let inner = tags[name], let id = inner {
                return json(["id": id], status: 200, url: url)
            }
            return raw("{\"error\":\"not found\"}", status: 404, url: url)
        }
        if urlStr.hasSuffix("/user/v1alpha1/tag") && method == "POST" {
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            let name = body["name"] as! String
            tagPosts.append(name)
            tagCreateSeq += 1
            return json(["id": "created-\(name)-\(tagCreateSeq - 1)"], status: 200, url: url)
        }
        if urlStr.hasSuffix("/user/v1alpha1/annotation") && method == "GET" {
            defGets += 1
            return json(defs, status: 200, url: url)
        }
        if urlStr.hasSuffix("/user/v1alpha1/annotation") && method == "POST" {
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            defPosts.append(body)
            return json(["id": defCreateId], status: 200, url: url)
        }
        throw EnsureDefinitionError("unexpected request: \(method) \(urlStr)")
    }
}

func defRow(_ id: String, _ type: String = "duration", deleted: String? = nil, created: String) -> [String: Any] {
    var r: [String: Any] = ["id": id, "name": "Attention", "annotation_type": type, "created_at": created]
    r["deleted_at"] = deleted as Any? ?? NSNull()
    return r
}

func makeEnsure(_ api: FakeApi, token: FakeToken = FakeToken(normal: "ACCESS"), cache: FakeCache = FakeCache()) -> (EnsureAttention, FakeCache) {
    let e = EnsureAttention(token: token, http: api, cache: cache, apiBase: FULCRA_API_BASE)
    return (e, cache)
}

// MARK: - Main

@main
struct ParityMain {
    static func main() async {
        let c = Checks()

        // ---- slugify golden cases (byte-parity with TS) ----
        c.eq(slugifyIdentity("Work MBP — Chrome"), "work-mbp-chrome", "slug: Work MBP — Chrome")
        c.eq(slugifyIdentity("ash@fulcra's laptop!!"), "ash-fulcra-s-laptop", "slug: ash@fulcra's laptop!!")
        c.eq(slugifyIdentity("  ---Hello___World---  "), "hello-world", "slug: ---Hello___World---")
        c.eq(slugifyIdentity("a   b"), "a-b", "slug: a   b")
        c.eq(slugifyIdentity(""), "browser", "slug: empty")
        c.eq(slugifyIdentity("   "), "browser", "slug: spaces")
        c.eq(slugifyIdentity("---"), "browser", "slug: dashes")
        // Multi-byte char case. JS: "café crème".toLowerCase()
        //   .replace(/[^a-z0-9]+/g,"-") → é and space are each non-[a-z0-9] →
        //   collapse to "-": "caf-cr-me" (the two é's become separators).
        c.eq(slugifyIdentity("café crème"), "caf-cr-me", "slug: café crème (multibyte → separators)")
        // machineTagName
        c.eq(machineTagName("Work MBP — Chrome"), "machine:work-mbp-chrome", "machineTag: Work MBP — Chrome")
        c.eq(machineTagName(""), "machine:browser", "machineTag: empty")
        // 30-char cap
        let longLabel = "this is a really really long browser label that exceeds the limit"
        let longSlug = slugifyIdentity(longLabel)
        c.ok(longSlug.count <= 22, "slug cap: long slug <= 22 (\(longSlug.count))")
        c.ok(!longSlug.hasSuffix("-"), "slug cap: no trailing dash")
        c.ok(machineTagName(longLabel).count <= 30, "tag cap: <= 30")
        // trailing-dash-at-boundary re-trim
        let boundary = slugifyIdentity("aaaaaaaaaaaaaaaaaaaaa bbbb") // 21 a's, sep at idx 21
        c.ok(boundary.count <= 22 && !boundary.hasSuffix("-"), "slug boundary re-trim: \(boundary)")
        // userid worst case
        let userid = "12345678-9abc-def0-1234-56789abcdef0"
        let uidTag = machineTagName("\(userid) browser")
        c.ok(uidTag.count <= 30 && !uidTag.hasSuffix("-"), "userid tag <= 30: \(uidTag)")

        // encodeURIComponent parity: ":" must be %3A
        c.eq(encodeURIComponentJS("machine:work-mbp-chrome"), "machine%3Awork-mbp-chrome", "encode: colon → %3A")

        // ---- ensure: adopt existing def (no def POST; tags found) ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"],
                              defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")])
            let (e, _) = makeEnsure(api)
            let res = try await e.ensureAttentionDefinitionAndTags()
            c.eq(res, ResolvedAttention(definitionId: "def-existing", tagIds: ["tag-attn", "tag-web"]), "ensure-adopt: result")
            c.eq(api.defPosts.count, 0, "ensure-adopt: no def POST")
            c.eq(api.tagGets, ["attention", "web"], "ensure-adopt: tagGets")
            c.eq(api.tagPosts.count, 0, "ensure-adopt: no tag POST")
        } catch { c.ok(false, "ensure-adopt threw: \(error)") }

        // ---- ensure: create def when none, canonical payload ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"], defs: [], defCreateId: "def-new")
            let (e, _) = makeEnsure(api)
            let res = try await e.ensureAttentionDefinitionAndTags()
            c.eq(res.definitionId, "def-new", "ensure-create: id")
            c.eq(res.tagIds, ["tag-attn", "tag-web"], "ensure-create: tagIds")
            c.eq(api.defPosts.count, 1, "ensure-create: one def POST")
            let p = api.defPosts.first ?? [:]
            c.eq(p["annotation_type"] as? String, "duration", "create payload: annotation_type")
            c.eq(p["name"] as? String, ATTENTION_DEFINITION_NAME, "create payload: name")
            c.eq(p["description"] as? String, ATTENTION_DEFINITION_DESCRIPTION, "create payload: description")
            c.eq(p["tags"] as? [String] ?? [], ["tag-attn", "tag-web"], "create payload: tags")
            let ms = p["measurement_spec"] as? [String: Any] ?? [:]
            c.eq(ms["measurement_type"] as? String, "duration", "create payload: measurement_type")
            c.eq(ms["value_type"] as? String, "duration", "create payload: value_type")
            c.ok(ms["unit"] is NSNull, "create payload: unit null")
        } catch { c.ok(false, "ensure-create threw: \(error)") }

        // ---- ensure: create only the MISSING tag ----
        do {
            let tags: [String: String?] = ["attention": nil, "web": "tag-web"]
            let api = FakeApi(tags: tags, defs: [])
            let (e, _) = makeEnsure(api)
            let res = try await e.ensureAttentionDefinitionAndTags()
            c.eq(api.tagGets, ["attention", "web"], "ensure-missing-tag: tagGets")
            c.eq(api.tagPosts, ["attention"], "ensure-missing-tag: only attention POSTed")
            c.ok(res.tagIds[0].hasPrefix("created-attention-"), "ensure-missing-tag: created id")
            c.eq(res.tagIds[1], "tag-web", "ensure-missing-tag: web existing")
        } catch { c.ok(false, "ensure-missing-tag threw: \(error)") }

        // ---- ensure: adopt OLDEST when multiple; ignore deleted/non-duration ----
        do {
            let api = FakeApi(tags: ["attention": "a", "web": "w"], defs: [
                defRow("deleted", deleted: "2026-02-02T00:00:00Z", created: "2026-01-01T00:00:00Z"),
                defRow("moment", "moment", created: "2026-01-01T00:00:00Z"),
                defRow("newer", created: "2026-03-01T00:00:00Z"),
                defRow("oldest", created: "2026-02-15T00:00:00Z"),
            ])
            let (e, _) = makeEnsure(api)
            let res = try await e.ensureAttentionDefinitionAndTags()
            c.eq(res.definitionId, "oldest", "ensure-oldest: picks oldest live duration")
            c.eq(api.defPosts.count, 0, "ensure-oldest: no def POST")
        } catch { c.ok(false, "ensure-oldest threw: \(error)") }

        // ---- created_at sort must match JS localeCompare, not raw Swift `<` ----
        do {
            let api = FakeApi(tags: ["attention": "a", "web": "w"], defs: [
                defRow("plus", created: "2026-01-01T00:00:00+01:00"),
                defRow("minus", created: "2026-01-01T00:00:00-05:00"),
            ])
            let (e, _) = makeEnsure(api)
            let res = try await e.ensureAttentionDefinitionAndTags()
            c.eq(res.definitionId, "minus", "ensure-localeCompare: '-' offset sorts before '+'")
        } catch { c.ok(false, "ensure-localeCompare threw: \(error)") }

        // ---- ensure: cache → second call makes NO new requests ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"],
                              defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")])
            let (e, _) = makeEnsure(api)
            let a = try await e.ensureAttentionDefinitionAndTags()
            let after = api.totalCalls
            c.ok(after > 0, "ensure-cache: first call hit network")
            let b = try await e.ensureAttentionDefinitionAndTags()
            c.eq(b, a, "ensure-cache: second equals first")
            c.eq(api.totalCalls, after, "ensure-cache: no new requests")
            c.eq(api.defGets, 1, "ensure-cache: one def GET total")
        } catch { c.ok(false, "ensure-cache threw: \(error)") }

        // ---- clear cache → re-resolves ----
        do {
            let api = FakeApi(tags: ["attention": "a", "web": "w"],
                              defs: [defRow("d1", created: "2026-01-01T00:00:00Z")])
            let (e, _) = makeEnsure(api)
            _ = try await e.ensureAttentionDefinitionAndTags()
            let n = api.totalCalls
            e.clearResolvedAttention()
            _ = try await e.ensureAttentionDefinitionAndTags()
            c.ok(api.totalCalls > n, "clear-cache: re-resolves (more calls)")
        } catch { c.ok(false, "clear-cache threw: \(error)") }

        // ---- not signed in → UnauthorizedError BEFORE any fetch ----
        do {
            let api = FakeApi()
            let (e, _) = makeEnsure(api, token: FakeToken(normal: nil))
            do {
                _ = try await e.ensureAttentionDefinitionAndTags()
                c.ok(false, "no-token: should have thrown")
            } catch is UnauthorizedError {
                c.eq(api.totalCalls, 0, "no-token: no fetch happened")
            } catch { c.ok(false, "no-token: wrong error \(error)") }
        }

        // ---- 401 on everything → still UnauthorizedError ----
        do {
            let api = FakeApi(gateToken: "NEVER") // nothing matches → always 401
            let token = FakeToken(normal: "STALE", forced: "FRESH")
            let (e, _) = makeEnsure(api, token: token)
            do {
                _ = try await e.ensureAttentionDefinitionAndTags()
                c.ok(false, "401-all: should have thrown")
            } catch is UnauthorizedError {
                c.ok(true, "401-all: UnauthorizedError")
            } catch { c.ok(false, "401-all: wrong error \(error)") }
        }

        // ---- 401 then force-refresh retry succeeds ----
        do {
            let api = FakeApi(tags: [:],
                              defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")],
                              gateToken: "FRESH")
            // make the tag GETs resolve to ids so tagIds == [id-attention,...]:
            // gate forces 401 until Bearer FRESH; once FRESH, tags are 404 →
            // create. To match the TS test (tagRow id-<name>), supply existing.
            api.tags = ["attention": "id-attention", "web": "id-web"]
            let token = FakeToken(normal: "STALE", forced: "FRESH")
            let (e, _) = makeEnsure(api, token: token)
            let res = try await e.ensureAttentionDefinitionAndTags()
            c.eq(res.definitionId, "def-existing", "401-retry: def id")
            c.eq(res.tagIds, ["id-attention", "id-web"], "401-retry: tagIds")
            c.ok(token.forceCalledWith.contains(true), "401-retry: forced refresh requested")
            c.eq(api.seenAuth.first, "Bearer STALE", "401-retry: first auth STALE")
            c.ok(api.seenAuth.contains("Bearer FRESH"), "401-retry: later auth FRESH")
        } catch { c.ok(false, "401-retry threw: \(error)") }

        // ---- listAttentionDestinations: oldest-first, index0 autopick ----
        do {
            let api = FakeApi(tags: [:], defs: [
                defRow("deleted", deleted: "2026-02-02T00:00:00Z", created: "2026-01-01T00:00:00Z"),
                defRow("moment", "moment", created: "2026-01-01T00:00:00Z"),
                ["id": "otherName", "name": "Focus", "annotation_type": "duration", "deleted_at": NSNull(), "created_at": "2026-01-01T00:00:00Z"],
                defRow("newer", created: "2026-03-01T00:00:00Z"),
                defRow("oldest", created: "2026-02-15T00:00:00Z"),
            ])
            let (e, _) = makeEnsure(api)
            let out = try await e.listAttentionDestinations()
            c.eq(out.map { $0.id }, ["oldest", "newer"], "list: oldest-first ids")
            c.eq(out.first?.isAutoPick, true, "list: index0 autopick")
            c.eq(out.first?.createdAt, "2026-02-15T00:00:00Z", "list: createdAt")
            c.eq(out.count > 1 ? out[1].isAutoPick : true, false, "list: index1 not autopick")
            c.eq(api.defGets, 1, "list: one GET")
        } catch { c.ok(false, "list threw: \(error)") }

        // ---- listAttentionDestinations: empty → [] ----
        do {
            let api = FakeApi(tags: [:], defs: [])
            let (e, _) = makeEnsure(api)
            let out = try await e.listAttentionDestinations()
            c.eq(out.count, 0, "list-empty: []")
        } catch { c.ok(false, "list-empty threw: \(error)") }

        // ---- chooseAttentionDestination: never POSTs def; caches ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"], defs: [])
            let cache = FakeCache()
            let (e, _) = makeEnsure(api, cache: cache)
            let res = try await e.chooseAttentionDestination(definitionId: "chosen-def")
            c.eq(res, ResolvedAttention(definitionId: "chosen-def", tagIds: ["tag-attn", "tag-web"]), "choose: result")
            c.eq(api.defPosts.count, 0, "choose: never POSTs def")
            c.eq(api.tagGets, ["attention", "web"], "choose: tagGets")
            let cached = try await e.ensureAttentionDefinitionAndTags()
            c.eq(cached, res, "choose: cached")
        } catch { c.ok(false, "choose threw: \(error)") }

        // ---- createAttentionDestination: POSTs def; caches; default name ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"], defs: [], defCreateId: "fresh-def")
            let cache = FakeCache()
            let (e, _) = makeEnsure(api, cache: cache)
            let res = try await e.createAttentionDestination(name: "My Attention")
            c.eq(res.definitionId, "fresh-def", "create-dest: id")
            c.eq(api.defPosts.count, 1, "create-dest: one POST")
            c.eq((api.defPosts.first?["name"] as? String), "My Attention", "create-dest: custom name")
            let cached = try await e.ensureAttentionDefinitionAndTags()
            c.eq(cached, res, "create-dest: cached")
        } catch { c.ok(false, "create-dest threw: \(error)") }
        do {
            let api = FakeApi(tags: ["attention": "a", "web": "w"], defs: [], defCreateId: "fresh-def")
            let (e, _) = makeEnsure(api)
            _ = try await e.createAttentionDestination()
            c.eq(api.defPosts.first?["name"] as? String, ATTENTION_DEFINITION_NAME, "create-dest: default name")
        } catch { c.ok(false, "create-dest-default threw: \(error)") }

        // ---- identity label appends machine tag ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web", "machine:work-mbp-chrome": "tag-machine"],
                              defs: [], defCreateId: "fresh-def")
            let (e, _) = makeEnsure(api)
            let res = try await e.createAttentionDestination(name: "My Attention", identityLabel: "Work MBP — Chrome")
            c.eq(res.tagIds, ["tag-attn", "tag-web", "tag-machine"], "identity: tagIds w/ machine")
            c.eq(api.tagGets, ["attention", "web", "machine:work-mbp-chrome"], "identity: tagGets w/ machine")
        } catch { c.ok(false, "identity threw: \(error)") }

        // ---- ensure identity null → no machine tag ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"],
                              defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")])
            let (e, _) = makeEnsure(api)
            let res = try await e.ensureAttentionDefinitionAndTags(identityLabel: nil)
            c.eq(res.tagIds, ["tag-attn", "tag-web"], "identity-null: no machine tag")
            c.eq(api.tagGets, ["attention", "web"], "identity-null: tagGets")
        } catch { c.ok(false, "identity-null threw: \(error)") }

        // ---- updateIdentity: no cache → nil, no fetch ----
        do {
            let api = FakeApi()
            let (e, _) = makeEnsure(api)
            let res = try await e.updateIdentity(label: "Whatever")
            c.ok(res == nil, "updateIdentity: nil when no cache")
            c.eq(api.totalCalls, 0, "updateIdentity: no fetch")
        } catch { c.ok(false, "updateIdentity-nil threw: \(error)") }

        // ---- updateIdentity: rewrites tagIds keeping definitionId ----
        do {
            let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web", "machine:new-name": "tag-mach"], defs: [])
            let cache = FakeCache()
            let (e, _) = makeEnsure(api, cache: cache)
            _ = try await e.chooseAttentionDestination(definitionId: "def-keep")
            let res = try await e.updateIdentity(label: "New Name")
            c.eq(res, ResolvedAttention(definitionId: "def-keep", tagIds: ["tag-attn", "tag-web", "tag-mach"]), "updateIdentity: rewrites")
            let cached = try await e.ensureAttentionDefinitionAndTags()
            c.eq(cached, res, "updateIdentity: cached")
        } catch { c.ok(false, "updateIdentity threw: \(error)") }

        let code = c.report()
        exit(code)
    }
}
