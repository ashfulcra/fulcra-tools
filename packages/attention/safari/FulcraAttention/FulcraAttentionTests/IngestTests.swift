//
//  IngestTests.swift
//  FulcraAttentionTests
//
//  Behaviour parity with chrome/tests/relayless/relaylessSender.test.ts. The
//  cases are written as the failures they prevent, because the expensive
//  mistakes here are all "sent-set says sent when it wasn't" and its mirror.
//
//  NOTE: at time of writing the Xcode project has NO test target, so these do
//  not run under `xcodebuild test`. They are written against XCTest and are
//  additionally exercised by scripts/run_swift_tests.sh, which compiles this
//  directory directly — see that script's header.
//

import XCTest
@testable import FulcraAttention

// MARK: - Doubles

/// In-memory sent-id store: the storage seam, without UserDefaults.
final class MemorySentIdStore: SentIdStore, @unchecked Sendable {
    private var ids: [String] = []
    init(_ seed: [String] = []) { ids = seed }
    func load() -> [String] { ids }
    func save(_ newIds: [String]) { ids = newIds }
    func removeAll() { ids = [] }
}

/// Token provider that records how it was called, so a test can prove the
/// forced-refresh path ran (or did not).
final class FakeTokenProvider: TokenProvider, @unchecked Sendable {
    var normal: String?
    var forced: String?
    var throwsOnForce = false
    private(set) var forceCalls = 0

    init(normal: String? = "tok", forced: String? = "tok2") {
        self.normal = normal
        self.forced = forced
    }

    func accessToken(forceRefresh: Bool) async throws -> String? {
        if forceRefresh {
            forceCalls += 1
            if throwsOnForce { throw UnauthorizedError("refresh failed") }
            return forced
        }
        return normal
    }
}

/// HTTP double returning a scripted sequence of statuses, capturing requests.
final class ScriptedHTTPClient: HTTPClient, @unchecked Sendable {
    private var statuses: [Int]
    var throwsTransportError = false
    private(set) var requests: [URLRequest] = []

    init(statuses: [Int]) { self.statuses = statuses }

    func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        if throwsTransportError { throw URLError(.notConnectedToInternet) }
        let status = statuses.isEmpty ? 200 : statuses.removeFirst()
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil
        )!
        return (Data(), response)
    }
}

// MARK: - Fixtures

private func makeEvent(url: String, start: String, end: String) -> AttentionEvent {
    AttentionEvent(
        url: url, title: "t", ogDescription: nil, faviconURL: nil,
        category: nil, chromeIdentity: nil, ogType: nil, lang: nil,
        startTime: start, endTime: end, client: "safari"
    )
}

private let ctx = WireContext(
    definitionId: "def-1", tagIds: ["tag-a", "tag-b"], identitySlug: "testmac"
)

// MARK: - Tests

final class IngestTests: XCTestCase {

    // ---- claim-then-record: the expensive direction ----

    func testAFailedPostLeavesTheSentSetUntouchedSoEventsRetry() async throws {
        // Marking ids sent before the POST would drop events PERMANENTLY on a
        // transient failure — they are gone from the queue and never retried.
        let store = MemorySentIdStore()
        let sender = RelaylessSender(
            token: FakeTokenProvider(),
            http: ScriptedHTTPClient(statuses: [500]),
            sentSet: SentSet(store: store),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z")],
            context: ctx
        )

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.failureStatus, 500)
        XCTAssertTrue(result.sent.isEmpty)
        XCTAssertEqual(store.load(), [], "a failed POST must not record ids as sent")
    }

    func testASuccessfulPostRecordsExactlyTheSentIds() async throws {
        let store = MemorySentIdStore()
        let sender = RelaylessSender(
            token: FakeTokenProvider(),
            http: ScriptedHTTPClient(statuses: [200]),
            sentSet: SentSet(store: store),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z")],
            context: ctx
        )

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.sent.count, 1)
        XCTAssertEqual(store.load(), result.sent)
    }

    // ---- dedup ----

    func testAlreadySentEventsAreSkippedAndNotReposted() async throws {
        let event = makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                              end: "2026-06-01T00:01:00.000Z")
        let sourceId = try! Wire.buildWireRecord(event: event, context: ctx).sourceId

        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: MemorySentIdStore([sourceId])),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch([event], context: ctx)

        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.skipped, [sourceId])
        XCTAssertTrue(result.sent.isEmpty)
        XCTAssertTrue(http.requests.isEmpty, "nothing to send must mean NO request at all")
    }

    func testADuplicateWithinOneFlushIsSentOnceAndIsNotReportedAsSkipped() async throws {
        // "skipped" means "already sent in an EARLIER flush" — a distinct fact
        // from "this batch carried the same event twice". Collapsing them would
        // make the counter lie about what happened.
        let event = makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                              end: "2026-06-01T00:01:00.000Z")
        let store = MemorySentIdStore()
        let sender = RelaylessSender(
            token: FakeTokenProvider(),
            http: ScriptedHTTPClient(statuses: [200]),
            sentSet: SentSet(store: store),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch([event, event], context: ctx)

        XCTAssertEqual(result.sent.count, 1)
        XCTAssertTrue(result.skipped.isEmpty)
        XCTAssertEqual(store.load().count, 1)
    }

    // ---- auth ----

    func testA401TriggersExactlyOneForcedRefreshAndRetry() async throws {
        let token = FakeTokenProvider()
        let http = ScriptedHTTPClient(statuses: [401, 200])
        let sender = RelaylessSender(
            token: token, http: http,
            sentSet: SentSet(store: MemorySentIdStore()),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z")],
            context: ctx
        )

        XCTAssertTrue(result.ok)
        XCTAssertEqual(token.forceCalls, 1, "exactly one forced refresh")
        XCTAssertEqual(http.requests.count, 2)
        let retryAuth = http.requests[1].value(forHTTPHeaderField: "Authorization")
        XCTAssertEqual(retryAuth, "Bearer tok2", "the retry must use the REFRESHED token")
    }

    func testAFailedForcedRefreshReportsUnauthorizedNotUnreachable() async throws {
        // 401 and 0 send the user to fix different things: sign in again vs
        // check the network. Conflating them is a real support cost.
        let token = FakeTokenProvider()
        token.throwsOnForce = true
        let sender = RelaylessSender(
            token: token,
            http: ScriptedHTTPClient(statuses: [401]),
            sentSet: SentSet(store: MemorySentIdStore()),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z")],
            context: ctx
        )

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.failureStatus, 401)
    }

    func testNotSignedInReportsZeroNotUnauthorized() async throws {
        let sender = RelaylessSender(
            token: FakeTokenProvider(normal: nil, forced: nil),
            http: ScriptedHTTPClient(statuses: [200]),
            sentSet: SentSet(store: MemorySentIdStore()),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z")],
            context: ctx
        )

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.failureStatus, 0)
    }

    func testATransportErrorReportsZeroAndKeepsTheSentSetClean() async throws {
        let store = MemorySentIdStore()
        let http = ScriptedHTTPClient(statuses: [200])
        http.throwsTransportError = true
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: store),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z")],
            context: ctx
        )

        XCTAssertFalse(result.ok)
        XCTAssertEqual(result.failureStatus, 0)
        XCTAssertEqual(store.load(), [])
    }

    // ---- wire contract ----

    func testThePostUsesTheJSONLContentTypeAndBearerAuth() async throws {
        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: MemorySentIdStore()),
            url: "https://example.invalid/batch"
        )

        _ = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z")],
            context: ctx
        )

        let req = http.requests[0]
        // The endpoint takes newline-delimited JSON, not a JSON array. Sending
        // application/json is rejected.
        XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/x-jsonl")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer tok")
        XCTAssertEqual(req.httpMethod, "POST")
    }

    func testTheBodyIsOneJSONLineOerRecord() async throws {
        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: MemorySentIdStore()),
            url: "https://example.invalid/batch"
        )

        _ = try await sender.sendBatch(
            [makeEvent(url: "https://a.example/1", start: "2026-06-01T00:00:00.000Z",
                       end: "2026-06-01T00:01:00.000Z"),
             makeEvent(url: "https://b.example/2", start: "2026-06-01T00:02:00.000Z",
                       end: "2026-06-01T00:03:00.000Z")],
            context: ctx
        )

        let body = String(data: http.requests[0].httpBody!, encoding: .utf8)!
        XCTAssertEqual(body.split(separator: "\n").count, 2)
        XCTAssertFalse(body.hasPrefix("["), "JSONL, not a JSON array")
    }

    // ---- empty flush ----

    func testAnEmptyBatchMakesNoRequest() async throws {
        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: MemorySentIdStore()),
            url: "https://example.invalid/batch"
        )

        let result = try await sender.sendBatch([], context: ctx)

        XCTAssertTrue(result.ok, "nothing to do is success, not failure")
        XCTAssertTrue(http.requests.isEmpty)
    }
}

// MARK: - SentSet

final class SentSetTests: XCTestCase {

    func testInsertionOrderIsPreservedAndTheOLDESTAreEvictedAtTheCap() async {
        // Swift's Set does not preserve insertion order, so a port that carried
        // only a Set through the flush would evict ARBITRARY ids here — which
        // looks fine in a unit test that only counts, and silently re-POSTs the
        // wrong things in production.
        let store = MemorySentIdStore()
        let set = SentSet(store: store, cap: 3)
        set.addMany(["a", "b", "c"])
        set.addMany(["d"])
        XCTAssertEqual(store.load(), ["b", "c", "d"])
    }

    func testAddingIsIdempotentAndDoesNotReorder() async {
        let store = MemorySentIdStore()
        let set = SentSet(store: store, cap: 10)
        set.addMany(["a", "b"])
        set.addMany(["a"])
        XCTAssertEqual(store.load(), ["a", "b"])
    }

    func testAnEmptyAddIsANoOpAndDoesNotWrite() async {
        let store = MemorySentIdStore(["a"])
        SentSet(store: store, cap: 10).addMany([])
        XCTAssertEqual(store.load(), ["a"])
    }

    func testClearDropsEverything() async {
        // Sign-out / account switch: source_id omits the account and the
        // definition, so a re-queued id from the previous account would
        // otherwise be skipped against the NEW account's definition.
        let store = MemorySentIdStore(["a", "b"])
        SentSet(store: store, cap: 10).clear()
        XCTAssertEqual(store.load(), [])
    }
}


// --------------------------------------------------------------------------
// ROUND 2 — codex-reviewer. A wire-build failure must never be reported as
// success: the caller clears the whole snapshot on ok, so an unaccounted-for
// event is a PERMANENTLY LOST event.
// --------------------------------------------------------------------------

/// An event whose timestamps cannot be parsed, so buildWireRecord throws.
private func makeUnbuildableEvent() -> AttentionEvent {
    AttentionEvent(
        url: "https://a.example/bad", title: "t", ogDescription: nil,
        faviconURL: nil, category: nil, chromeIdentity: nil, ogType: nil,
        lang: nil, startTime: "not-a-timestamp", endTime: "also-not-one",
        client: "safari"
    )
}

final class IngestUnbuildableTests: XCTestCase {

    func testAnUnbuildableEventIsNotSilentlyDropped() async throws {
        // The whole finding in one case. Previously this returned ok:true with
        // the bad event in neither `sent` nor `skipped` — so a caller that
        // clears its outbox on ok would discard an event that was never posted
        // and never reported.
        let store = MemorySentIdStore()
        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: store), url: "https://example.invalid/batch"
        )

        do {
            _ = try await sender.sendBatch([makeUnbuildableEvent()], context: ctx)
            XCTFail("an unbuildable event must fail the flush, not return success")
        } catch {
            // expected
        }
        XCTAssertEqual(store.load(), [], "nothing may be recorded as sent")
        XCTAssertTrue(http.requests.isEmpty, "nothing may be posted")
    }

    func testAMixedBatchFailsTheWholeFlushSoNoEventIsLost() async throws {
        // The dangerous shape codex named: the good events succeed, the flush
        // reports ok, and the bad one vanishes with the cleared snapshot.
        // Matching the TypeScript, the throw aborts the flush BEFORE any POST,
        // so the caller retries the whole batch with its outbox intact.
        let store = MemorySentIdStore()
        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: store), url: "https://example.invalid/batch"
        )

        let good = makeEvent(url: "https://a.example/1",
                             start: "2026-06-01T00:00:00.000Z",
                             end: "2026-06-01T00:01:00.000Z")

        do {
            _ = try await sender.sendBatch([good, makeUnbuildableEvent()], context: ctx)
            XCTFail("a mixed batch must fail the flush")
        } catch {
            // expected
        }
        XCTAssertEqual(store.load(), [], "a failed flush records nothing as sent")
        XCTAssertTrue(http.requests.isEmpty, "no partial POST")
    }

    func testAnAllInvalidBatchFailsRatherThanReportingAnEmptySuccess() async throws {
        // Every event unbuildable used to produce toSend == [] and therefore
        // the "nothing to do is success" path — the most misleading possible
        // answer, since the caller then clears a snapshot full of live events.
        let store = MemorySentIdStore()
        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: store), url: "https://example.invalid/batch"
        )

        do {
            _ = try await sender.sendBatch(
                [makeUnbuildableEvent(), makeUnbuildableEvent()], context: ctx)
            XCTFail("an all-invalid batch must fail, not report empty success")
        } catch {
            // expected
        }
        XCTAssertEqual(store.load(), [])
        XCTAssertTrue(http.requests.isEmpty)
    }

    func testAGenuinelyEmptyBatchIsStillSuccess() async {
        // The distinction that must survive the fix: "no events" is success;
        // "events I could not account for" is not.
        let http = ScriptedHTTPClient(statuses: [200])
        let sender = RelaylessSender(
            token: FakeTokenProvider(), http: http,
            sentSet: SentSet(store: MemorySentIdStore()),
            url: "https://example.invalid/batch"
        )

        let result = try? await sender.sendBatch([], context: ctx)
        XCTAssertEqual(result?.ok, true)
        XCTAssertTrue(http.requests.isEmpty)
    }
}
