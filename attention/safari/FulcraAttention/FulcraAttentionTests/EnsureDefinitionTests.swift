//
//  EnsureDefinitionTests.swift
//  FulcraAttentionTests
//
//  XCTest port of chrome/tests/relayless/ensureDefinition.test.ts.
//
//  NOTE: the xcodeproj currently has NO XCTest target, so these tests are not
//  wired into a scheme yet. The same behavioral contract is exercised
//  standalone (compilable via `swiftc`) in EnsureDefinitionParityCheck.swift,
//  which is what is actually run today. This file is kept in sync so that, once
//  a `FulcraAttentionTests` unit-test target is added to the project, the suite
//  runs under `xcodebuild test` unchanged.
//
//  Uses in-memory fakes for the token provider, HTTP transport, and resolved-id
//  cache (the EnsureAttention dependencies are all injectable).
//

import XCTest
@testable import FulcraAttention

// MARK: - Fakes

private final class FakeToken: TokenProvider, @unchecked Sendable {
    let normal: String?
    let forced: String?
    var forceCalledWith: [Bool] = []
    init(normal: String?, forced: String? = nil) { self.normal = normal; self.forced = forced }
    func accessToken(forceRefresh: Bool) async throws -> String? {
        forceCalledWith.append(forceRefresh)
        return forceRefresh ? (forced ?? normal) : normal
    }
}

private final class FakeCache: ResolvedAttentionCache, @unchecked Sendable {
    private var value: ResolvedAttention?
    func read() -> ResolvedAttention? { value }
    func write(_ resolved: ResolvedAttention) { value = resolved }
    func clear() { value = nil }
}

private final class FakeApi: HTTPClient, @unchecked Sendable {
    var tags: [String: String?]
    var defs: [[String: Any]]
    var defCreateId: String
    var gateToken: String?
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
        let data = try! JSONSerialization.data(withJSONObject: obj)
        return (data, HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }
    private func raw(_ s: String, status: Int, url: URL) -> (Data, HTTPURLResponse) {
        (s.data(using: .utf8)!, HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!)
    }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        totalCalls += 1
        let auth = request.value(forHTTPHeaderField: "Authorization") ?? ""
        seenAuth.append(auth)
        let url = request.url!
        let urlStr = url.absoluteString
        let method = (request.httpMethod ?? "GET").uppercased()
        if let gate = gateToken, auth != "Bearer \(gate)" { return raw("", status: 401, url: url) }
        if urlStr.contains("/user/v1alpha1/tag/name/") {
            let encoded = urlStr.components(separatedBy: "/tag/name/")[1]
            let name = encoded.removingPercentEncoding ?? encoded
            tagGets.append(name)
            if let inner = tags[name], let id = inner { return json(["id": id], status: 200, url: url) }
            return raw("{\"error\":\"not found\"}", status: 404, url: url)
        }
        if urlStr.hasSuffix("/user/v1alpha1/tag") && method == "POST" {
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            let name = body["name"] as! String
            tagPosts.append(name); tagCreateSeq += 1
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

private func defRow(_ id: String, _ type: String = "duration", deleted: String? = nil, created: String) -> [String: Any] {
    var r: [String: Any] = ["id": id, "name": "Attention", "annotation_type": type, "created_at": created]
    r["deleted_at"] = deleted as Any? ?? NSNull()
    return r
}

private func makeEnsure(_ api: FakeApi, token: FakeToken = FakeToken(normal: "ACCESS"), cache: FakeCache = FakeCache()) -> EnsureAttention {
    EnsureAttention(token: token, http: api, cache: cache, apiBase: FULCRA_API_BASE)
}

// MARK: - Tests

final class SlugifyTests: XCTestCase {
    func testGoldenCases() {
        XCTAssertEqual(slugifyIdentity("Work MBP — Chrome"), "work-mbp-chrome")
        XCTAssertEqual(slugifyIdentity("ash@fulcra's laptop!!"), "ash-fulcra-s-laptop")
        XCTAssertEqual(slugifyIdentity("  ---Hello___World---  "), "hello-world")
        XCTAssertEqual(slugifyIdentity("a   b"), "a-b")
        XCTAssertEqual(slugifyIdentity(""), "browser")
        XCTAssertEqual(slugifyIdentity("   "), "browser")
        XCTAssertEqual(slugifyIdentity("---"), "browser")
        // Multi-byte: é and space are each non-[a-z0-9] → separators (JS parity).
        XCTAssertEqual(slugifyIdentity("café crème"), "caf-cr-me")
    }
    func testMachineTagName() {
        XCTAssertEqual(machineTagName("Work MBP — Chrome"), "machine:work-mbp-chrome")
        XCTAssertEqual(machineTagName(""), "machine:browser")
    }
    func testThirtyCharCap() {
        let long = "this is a really really long browser label that exceeds the limit"
        XCTAssertLessThanOrEqual(slugifyIdentity(long).count, 22)
        XCTAssertFalse(slugifyIdentity(long).hasSuffix("-"))
        XCTAssertLessThanOrEqual(machineTagName(long).count, 30)
        let boundary = slugifyIdentity("aaaaaaaaaaaaaaaaaaaaa bbbb")
        XCTAssertLessThanOrEqual(boundary.count, 22)
        XCTAssertFalse(boundary.hasSuffix("-"))
        let tag = machineTagName("12345678-9abc-def0-1234-56789abcdef0 browser")
        XCTAssertLessThanOrEqual(tag.count, 30)
        XCTAssertFalse(tag.hasSuffix("-"))
    }
    func testEncodeColon() {
        XCTAssertEqual(encodeURIComponentJS("machine:work-mbp-chrome"), "machine%3Awork-mbp-chrome")
    }
}

final class EnsureAttentionTests: XCTestCase {
    func testAdoptExisting() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"],
                          defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")])
        let res = try await makeEnsure(api).ensureAttentionDefinitionAndTags()
        XCTAssertEqual(res, ResolvedAttention(definitionId: "def-existing", tagIds: ["tag-attn", "tag-web"]))
        XCTAssertEqual(api.defPosts.count, 0)
        XCTAssertEqual(api.tagGets, ["attention", "web"])
        XCTAssertEqual(api.tagPosts.count, 0)
    }

    func testCreateWhenNone() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"], defs: [], defCreateId: "def-new")
        let res = try await makeEnsure(api).ensureAttentionDefinitionAndTags()
        XCTAssertEqual(res.definitionId, "def-new")
        XCTAssertEqual(res.tagIds, ["tag-attn", "tag-web"])
        XCTAssertEqual(api.defPosts.count, 1)
        let p = api.defPosts[0]
        XCTAssertEqual(p["annotation_type"] as? String, "duration")
        XCTAssertEqual(p["name"] as? String, ATTENTION_DEFINITION_NAME)
        XCTAssertEqual(p["description"] as? String, ATTENTION_DEFINITION_DESCRIPTION)
        XCTAssertEqual(p["tags"] as? [String], ["tag-attn", "tag-web"])
        let ms = p["measurement_spec"] as? [String: Any]
        XCTAssertEqual(ms?["measurement_type"] as? String, "duration")
        XCTAssertEqual(ms?["value_type"] as? String, "duration")
        XCTAssertTrue(ms?["unit"] is NSNull)
    }

    func testCreatesMissingTagOnly() async throws {
        let tags: [String: String?] = ["attention": nil, "web": "tag-web"]
        let api = FakeApi(tags: tags, defs: [])
        let res = try await makeEnsure(api).ensureAttentionDefinitionAndTags()
        XCTAssertEqual(api.tagGets, ["attention", "web"])
        XCTAssertEqual(api.tagPosts, ["attention"])
        XCTAssertTrue(res.tagIds[0].hasPrefix("created-attention-"))
        XCTAssertEqual(res.tagIds[1], "tag-web")
    }

    func testAdoptsOldest() async throws {
        let api = FakeApi(tags: ["attention": "a", "web": "w"], defs: [
            defRow("deleted", deleted: "2026-02-02T00:00:00Z", created: "2026-01-01T00:00:00Z"),
            defRow("moment", "moment", created: "2026-01-01T00:00:00Z"),
            defRow("newer", created: "2026-03-01T00:00:00Z"),
            defRow("oldest", created: "2026-02-15T00:00:00Z"),
        ])
        let res = try await makeEnsure(api).ensureAttentionDefinitionAndTags()
        XCTAssertEqual(res.definitionId, "oldest")
        XCTAssertEqual(api.defPosts.count, 0)
    }

    func testCachesSecondCallNoNetwork() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"],
                          defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")])
        let e = makeEnsure(api)
        let a = try await e.ensureAttentionDefinitionAndTags()
        let after = api.totalCalls
        XCTAssertGreaterThan(after, 0)
        let b = try await e.ensureAttentionDefinitionAndTags()
        XCTAssertEqual(b, a)
        XCTAssertEqual(api.totalCalls, after)
        XCTAssertEqual(api.defGets, 1)
    }

    func testClearForcesReResolution() async throws {
        let api = FakeApi(tags: ["attention": "a", "web": "w"],
                          defs: [defRow("d1", created: "2026-01-01T00:00:00Z")])
        let e = makeEnsure(api)
        _ = try await e.ensureAttentionDefinitionAndTags()
        let n = api.totalCalls
        e.clearResolvedAttention()
        _ = try await e.ensureAttentionDefinitionAndTags()
        XCTAssertGreaterThan(api.totalCalls, n)
    }

    func testNotSignedInThrowsBeforeFetch() async {
        let api = FakeApi()
        let e = makeEnsure(api, token: FakeToken(normal: nil))
        do {
            _ = try await e.ensureAttentionDefinitionAndTags()
            XCTFail("expected UnauthorizedError")
        } catch is UnauthorizedError {
            XCTAssertEqual(api.totalCalls, 0)
        } catch { XCTFail("wrong error \(error)") }
    }

    func test401EverywhereThrows() async {
        let api = FakeApi(gateToken: "NEVER")
        let e = makeEnsure(api, token: FakeToken(normal: "STALE", forced: "FRESH"))
        do {
            _ = try await e.ensureAttentionDefinitionAndTags()
            XCTFail("expected UnauthorizedError")
        } catch is UnauthorizedError {
        } catch { XCTFail("wrong error \(error)") }
    }

    func test401ThenForceRefreshRetrySucceeds() async throws {
        let api = FakeApi(tags: ["attention": "id-attention", "web": "id-web"],
                          defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")],
                          gateToken: "FRESH")
        let token = FakeToken(normal: "STALE", forced: "FRESH")
        let res = try await makeEnsure(api, token: token).ensureAttentionDefinitionAndTags()
        XCTAssertEqual(res.definitionId, "def-existing")
        XCTAssertEqual(res.tagIds, ["id-attention", "id-web"])
        XCTAssertTrue(token.forceCalledWith.contains(true))
        XCTAssertEqual(api.seenAuth.first, "Bearer STALE")
        XCTAssertTrue(api.seenAuth.contains("Bearer FRESH"))
    }

    func testIdentityLabelAppendsMachineTag() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web", "machine:home-imac": "tag-mach"],
                          defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")])
        let res = try await makeEnsure(api).ensureAttentionDefinitionAndTags(identityLabel: "Home iMac")
        XCTAssertEqual(res.tagIds, ["tag-attn", "tag-web", "tag-mach"])
        XCTAssertEqual(api.tagGets, ["attention", "web", "machine:home-imac"])
    }

    func testIdentityNullNoMachineTag() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"],
                          defs: [defRow("def-existing", created: "2026-01-01T00:00:00Z")])
        let res = try await makeEnsure(api).ensureAttentionDefinitionAndTags(identityLabel: nil)
        XCTAssertEqual(res.tagIds, ["tag-attn", "tag-web"])
        XCTAssertEqual(api.tagGets, ["attention", "web"])
    }
}

final class ListChooseCreateUpdateTests: XCTestCase {
    func testListOldestFirstAutopick() async throws {
        let api = FakeApi(tags: [:], defs: [
            defRow("deleted", deleted: "2026-02-02T00:00:00Z", created: "2026-01-01T00:00:00Z"),
            defRow("moment", "moment", created: "2026-01-01T00:00:00Z"),
            ["id": "otherName", "name": "Focus", "annotation_type": "duration", "deleted_at": NSNull(), "created_at": "2026-01-01T00:00:00Z"],
            defRow("newer", created: "2026-03-01T00:00:00Z"),
            defRow("oldest", created: "2026-02-15T00:00:00Z"),
        ])
        let out = try await makeEnsure(api).listAttentionDestinations()
        XCTAssertEqual(out.map { $0.id }, ["oldest", "newer"])
        XCTAssertEqual(out[0], AttentionDestination(id: "oldest", name: "Attention", createdAt: "2026-02-15T00:00:00Z", isAutoPick: true))
        XCTAssertFalse(out[1].isAutoPick)
        XCTAssertEqual(api.defGets, 1)
    }

    func testListEmpty() async throws {
        let api = FakeApi(tags: [:], defs: [])
        let out = try await makeEnsure(api).listAttentionDestinations()
        XCTAssertEqual(out.count, 0)
    }

    func testChooseNeverPostsAndCaches() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"], defs: [])
        let cache = FakeCache()
        let e = makeEnsure(api, cache: cache)
        let res = try await e.chooseAttentionDestination(definitionId: "chosen-def")
        XCTAssertEqual(res, ResolvedAttention(definitionId: "chosen-def", tagIds: ["tag-attn", "tag-web"]))
        XCTAssertEqual(api.defPosts.count, 0)
        XCTAssertEqual(api.tagGets, ["attention", "web"])
        let cached = try await e.ensureAttentionDefinitionAndTags()
        XCTAssertEqual(cached, res)
    }

    func testChooseWithLabel() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web", "machine:work-laptop": "tag-mach"], defs: [])
        let res = try await makeEnsure(api).chooseAttentionDestination(definitionId: "chosen-def", identityLabel: "Work Laptop")
        XCTAssertEqual(res, ResolvedAttention(definitionId: "chosen-def", tagIds: ["tag-attn", "tag-web", "tag-mach"]))
        XCTAssertEqual(api.tagGets, ["attention", "web", "machine:work-laptop"])
    }

    func testCreateDestinationPostsAndCaches() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"], defs: [], defCreateId: "fresh-def")
        let cache = FakeCache()
        let e = makeEnsure(api, cache: cache)
        let res = try await e.createAttentionDestination(name: "My Attention")
        XCTAssertEqual(res.definitionId, "fresh-def")
        XCTAssertEqual(res.tagIds, ["tag-attn", "tag-web"])
        XCTAssertEqual(api.defPosts.count, 1)
        XCTAssertEqual(api.defPosts[0]["name"] as? String, "My Attention")
        let cached = try await e.ensureAttentionDefinitionAndTags()
        XCTAssertEqual(cached, res)
    }

    func testCreateDestinationDefaultName() async throws {
        let api = FakeApi(tags: ["attention": "a", "web": "w"], defs: [], defCreateId: "fresh-def")
        let e = makeEnsure(api)
        _ = try await e.createAttentionDestination()
        XCTAssertEqual(api.defPosts[0]["name"] as? String, ATTENTION_DEFINITION_NAME)
    }

    func testCreateDestinationWithLabel() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web", "machine:work-mbp-chrome": "tag-machine"],
                          defs: [], defCreateId: "fresh-def")
        let res = try await makeEnsure(api).createAttentionDestination(name: "My Attention", identityLabel: "Work MBP — Chrome")
        XCTAssertEqual(res.tagIds, ["tag-attn", "tag-web", "tag-machine"])
        XCTAssertEqual(api.tagGets, ["attention", "web", "machine:work-mbp-chrome"])
    }

    func testUpdateIdentityNoCacheReturnsNil() async throws {
        let api = FakeApi()
        let e = makeEnsure(api)
        let res = try await e.updateIdentity(label: "Whatever")
        XCTAssertNil(res)
        XCTAssertEqual(api.totalCalls, 0)
    }

    func testUpdateIdentityRewritesKeepingId() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web", "machine:new-name": "tag-mach"], defs: [])
        let cache = FakeCache()
        let e = makeEnsure(api, cache: cache)
        _ = try await e.chooseAttentionDestination(definitionId: "def-keep")
        let res = try await e.updateIdentity(label: "New Name")
        XCTAssertEqual(res, ResolvedAttention(definitionId: "def-keep", tagIds: ["tag-attn", "tag-web", "tag-mach"]))
        let cached = try await e.ensureAttentionDefinitionAndTags()
        XCTAssertEqual(cached, res)
    }

    func testUpdateIdentityEmptyLabel() async throws {
        let api = FakeApi(tags: ["attention": "tag-attn", "web": "tag-web"], defs: [])
        let cache = FakeCache()
        let e = makeEnsure(api, cache: cache)
        _ = try await e.chooseAttentionDestination(definitionId: "def-keep", identityLabel: "Old")
        let res = try await e.updateIdentity(label: "")
        XCTAssertEqual(res?.tagIds, ["tag-attn", "tag-web"])
        XCTAssertEqual(res?.definitionId, "def-keep")
    }
}
