//
//  NativeBridgeTests.swift
//  FulcraAttentionTests
//
//  The bridge is the seam between two codebases that do not share a type
//  system, so every case here is a way the JS side and the native side could
//  quietly disagree — and quiet disagreement at a process boundary is the
//  failure mode this project has paid for repeatedly.
//

import XCTest
@testable import FulcraAttention

private let bridgeCtx = WireContext(
    definitionId: "def-1", tagIds: ["t1"], identitySlug: "testmac"
)

/// A valid event in the JS WIRE shape — snake_case, which is NOT the Swift
/// property spelling. If these keys drift from chrome/src/types.ts the bridge
/// silently decodes nils, so the literal keys are the point of the fixture.
private func wireEvent(
    start: String = "2026-06-01T00:00:00.000Z",
    end: String = "2026-06-01T00:01:00.000Z"
) -> [String: Any] {
    [
        "url": "https://a.example/1",
        "title": "t",
        "og_description": NSNull(),
        "favicon_url": NSNull(),
        "category": NSNull(),
        "chrome_identity": NSNull(),
        "og_type": NSNull(),
        "lang": NSNull(),
        "start_time": start,
        "end_time": end,
        "client": "fulcra-attention-safari/0.1.0",
    ]
}

private func makeBridge(
    sender: RelaylessSender?,
    context: @escaping @Sendable () async throws -> WireContext = { bridgeCtx }
) -> NativeBridge {
    NativeBridge(sender: { sender }, context: context)
}

private func workingSender(
    store: MemorySentIdStore = MemorySentIdStore(),
    http: ScriptedHTTPClient = ScriptedHTTPClient(statuses: [200])
) -> RelaylessSender {
    RelaylessSender(token: FakeTokenProvider(), http: http,
                    sentSet: SentSet(store: store),
                    url: "https://example.invalid/batch")
}

final class NativeBridgeDecodeTests: XCTestCase {

    func testTheDecoderReadsTheJSWireKeysNotTheSwiftPropertyNames() throws {
        // og_description, favicon_url, chrome_identity, og_type and start_time
        // are the TS names. Decoding against Swift's camelCase spelling would
        // yield an event with everything nil and a missing timestamp — which is
        // exactly the sort of well-formed wrong answer nothing else would catch.
        let event = try BridgeDecode.event(from: wireEvent())
        XCTAssertEqual(event.startTime, "2026-06-01T00:00:00.000Z")
        XCTAssertEqual(event.endTime, "2026-06-01T00:01:00.000Z")
        XCTAssertEqual(event.client, "fulcra-attention-safari/0.1.0")
        XCTAssertEqual(event.url, "https://a.example/1")
    }

    func testJSONNullBecomesNilRatherThanAStringNamedNull() throws {
        // JSON null crosses as NSNull, which is not nil in Swift; a naive
        // `as? String` cast is fine but `raw[key] != nil` is not.
        let event = try BridgeDecode.event(from: wireEvent())
        XCTAssertNil(event.ogDescription)
        XCTAssertNil(event.category)
    }

    func testAMissingRequiredFieldIsRejectedRatherThanDefaulted() {
        // A defaulted timestamp produces a well-formed record describing a
        // moment that never happened — worse than a rejected batch, which at
        // least tells someone.
        for missing in ["start_time", "end_time", "client"] {
            var raw = wireEvent()
            raw.removeValue(forKey: missing)
            XCTAssertThrowsError(try BridgeDecode.event(from: raw),
                                 "missing \(missing) must throw")
        }
    }

    func testAnEmptyRequiredFieldIsAlsoRejected() {
        var raw = wireEvent()
        raw["start_time"] = ""
        XCTAssertThrowsError(try BridgeDecode.event(from: raw))
    }
}

final class NativeBridgeHandleTests: XCTestCase {

    func testAnUnknownMessageTypeIsRefusedNotIgnored() async {
        let reply = await makeBridge(sender: workingSender())
            .handle(["type": "definitely-not-a-real-type"])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, false)
        XCTAssertNotNil(reply[BridgeKey.error])
    }

    func testAMessageWithNoTypeIsRefused() async {
        let reply = await makeBridge(sender: workingSender()).handle([:])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, false)
    }

    func testAGoodBatchIsIngestedAndCounted() async {
        let store = MemorySentIdStore()
        let reply = await makeBridge(sender: workingSender(store: store))
            .handle(["type": "ingest", "events": [wireEvent()]])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, true)
        XCTAssertEqual(reply[BridgeKey.sent] as? Int, 1)
        XCTAssertEqual(store.load().count, 1, "the send must have really happened")
    }

    func testOneBadEventFailsTheWholeBatchAndIngestsNothing() async {
        // All-or-nothing on purpose: the JS side still holds these events, and
        // partially accepting the batch while reporting success is how the
        // rejected one would be lost for good (same contract as PR 601 r1).
        let store = MemorySentIdStore()
        let http = ScriptedHTTPClient(statuses: [200])
        var bad = wireEvent(); bad.removeValue(forKey: "start_time")

        let reply = await makeBridge(sender: workingSender(store: store, http: http))
            .handle(["type": "ingest", "events": [wireEvent(), bad]])

        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, false)
        XCTAssertEqual(store.load(), [], "nothing may be recorded as sent")
        XCTAssertTrue(http.requests.isEmpty, "no partial POST")
    }

    func testAMalformedEventsFieldIsReportedNotTreatedAsEmpty() async {
        // "events": "oops" must NOT read as an empty batch, which would return
        // success over a message that carried real data.
        let reply = await makeBridge(sender: workingSender())
            .handle(["type": "ingest", "events": "oops"])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, false)
    }

    func testNoTokenReportsNeedsSignInDistinctlyFromAnError() async {
        // The extension cannot read the app's token until the shared-keychain
        // entitlement is registered. That must be legible to the JS side as
        // "prompt a sign-in", not as a generic failure it would back off from.
        let reply = await makeBridge(sender: nil)
            .handle(["type": "ingest", "events": [wireEvent()]])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, false)
        XCTAssertEqual(reply[BridgeKey.needsSignIn] as? Bool, true)
    }

    func testAuthStateReportsWhetherATokenIsVisible() async {
        var reply = await makeBridge(sender: nil).handle(["type": "auth_state"])
        XCTAssertEqual(reply[BridgeKey.signedIn] as? Bool, false)

        reply = await makeBridge(sender: workingSender()).handle(["type": "auth_state"])
        XCTAssertEqual(reply[BridgeKey.signedIn] as? Bool, true)
    }

    func testAFailedContextResolutionIsReportedNotSwallowed() async {
        // Resolving the definition hits the network and can 401. The batch must
        // not be reported as sent when we never got as far as sending it.
        let store = MemorySentIdStore()
        let bridge = makeBridge(sender: workingSender(store: store),
                                context: { throw UnauthorizedError("expired") })
        let reply = await bridge.handle(["type": "ingest", "events": [wireEvent()]])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, false)
        XCTAssertEqual(reply[BridgeKey.needsSignIn] as? Bool, true)
        XCTAssertEqual(store.load(), [])
    }

    func testAnHTTPFailureIsReportedAsNotOk() async {
        let store = MemorySentIdStore()
        let reply = await makeBridge(
            sender: workingSender(store: store,
                                  http: ScriptedHTTPClient(statuses: [500]))
        ).handle(["type": "ingest", "events": [wireEvent()]])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, false)
        XCTAssertEqual(store.load(), [], "a failed POST records nothing")
    }

    func testAnEmptyBatchIsSuccess() async {
        let reply = await makeBridge(sender: workingSender())
            .handle(["type": "ingest", "events": [[String: Any]]()])
        XCTAssertEqual(reply[BridgeKey.ok] as? Bool, true)
        XCTAssertEqual(reply[BridgeKey.sent] as? Int, 0)
    }
}
