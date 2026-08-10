//
//  Ingest.swift
//  FulcraAttention
//
//  Swift port of chrome/src/relayless/relaylessSender.ts — step 6 of the
//  native track (docs/proposals/2026-06-04-relayless-and-mobile-safari-attention.md).
//
//  Composes the pieces already landed: Wire.buildWireRecord / Wire.encodeBatch
//  (#93), the resolved {definitionId, tagIds} from EnsureDefinition (#94), the
//  Keychain-backed token via TokenProvider (#91), and SentSet for dedup.
//
//  CLAIM-THEN-RECORD (mirrors the daemon and the TS sender): within one flush an
//  event is sent at most once — the batch is de-duped by source_id BEFORE the
//  POST — and ids are marked sent only AFTER a 2xx. A non-2xx therefore leaves
//  the sent-set untouched and the ids retry on the next flush. Marking before
//  the POST would drop events permanently on a transient failure.
//
//  Never throws on an HTTP failure: the outcome is reported in the result so a
//  flush loop owns retry timing. Transport errors surface as ok == false.
//

import Foundation
import os

private let ingestLog = Logger(subsystem: "com.fulcra.attention", category: "ingest")

/// The batch ingest endpoint. Mirrors `INGEST_BATCH_URL` in relayless/config.ts.
public let INGEST_BATCH_URL = "\(FULCRA_API_BASE)/ingest/v1/record/batch"

/// The batch body is newline-delimited JSON, NOT a JSON array — the endpoint
/// requires this exact content type.
public let INGEST_CONTENT_TYPE = "application/x-jsonl"

/// Outcome of one `sendBatch` flush. Mirrors the TS `SendBatchResult`.
public struct SendBatchResult: Equatable, Sendable {
    /// source_ids successfully POSTed this flush.
    public let sent: [String]
    /// source_ids skipped because they were already in the sent-set.
    public let skipped: [String]
    /// True if the POST succeeded, or there was nothing to send.
    public let ok: Bool
    /// Set when the POST failed; carries the HTTP status.
    /// **0 means transport error or not-signed-in**, not "success".
    public let failureStatus: Int?

    public init(sent: [String], skipped: [String], ok: Bool, failureStatus: Int? = nil) {
        self.sent = sent
        self.skipped = skipped
        self.ok = ok
        self.failureStatus = failureStatus
    }
}

/// The relayless ingest sender.
public struct RelaylessSender: Sendable {
    private let token: TokenProvider
    private let http: HTTPClient
    private let sentSet: SentSet
    private let url: String

    public init(
        token: TokenProvider,
        http: HTTPClient = URLSessionHTTPClient(),
        sentSet: SentSet = SentSet(),
        url: String = INGEST_BATCH_URL
    ) {
        self.token = token
        self.http = http
        self.sentSet = sentSet
        self.url = url
    }

    /// Build records for `events`, skip already-sent ones, POST the rest, and
    /// record their source_ids on success.
    public func sendBatch(
        _ events: [AttentionEvent],
        context: WireContext
    ) async -> SendBatchResult {
        var skipped: [String] = []
        var toSend: [(record: WireRecord, sourceId: String)] = []

        // ONE read for the flush: membership checks below run against this
        // in-memory snapshot rather than hitting storage per event, and the same
        // snapshot is handed back to addMany so the write needs no second read.
        let snapshot = sentSet.snapshot()
        // Seeded from the snapshot so a previously-sent id is both skipped AND
        // not re-claimed.
        var claimed = snapshot.membership

        for event in events {
            let result: WireResult
            do {
                result = try Wire.buildWireRecord(event: event, context: context)
            } catch {
                // An unparseable timestamp is a defect in ONE event. Dropping it
                // keeps the rest of the flush deliverable; failing the batch
                // would let a single malformed event block every other event
                // behind it, indefinitely.
                ingestLog.error("sendBatch: skipping unbuildable event: \(String(describing: error))")
                continue
            }
            if snapshot.membership.contains(result.sourceId) {
                skipped.append(result.sourceId)
                continue
            }
            // Duplicate WITHIN this flush: already claimed above, so drop it
            // silently. Deliberately not counted as "skipped" — skipped means
            // "we had already sent this in an earlier flush", which is a
            // different fact about the world.
            if claimed.contains(result.sourceId) { continue }
            claimed.insert(result.sourceId)
            toSend.append((result.record, result.sourceId))
        }

        guard !toSend.isEmpty else {
            return SendBatchResult(sent: [], skipped: skipped, ok: true)
        }

        let body = Wire.encodeBatch(toSend.map(\.record))

        // First attempt with the cached token; on 401 refresh once and retry.
        var status = await postBatch(body: body, force: false)
        if status == 401 {
            ingestLog.info("sendBatch: 401; one forced token refresh + retry")
            status = await postBatch(body: body, force: true)
        }

        guard (200..<300).contains(status) else {
            ingestLog.error("sendBatch: POST failed with status \(status); sent-set untouched")
            return SendBatchResult(sent: [], skipped: skipped, ok: false, failureStatus: status)
        }

        let ids = toSend.map(\.sourceId)
        // One write for the whole flush, merged and capped, reusing the snapshot
        // as `known`. Only 2xx ids land here.
        sentSet.addMany(ids, known: snapshot)
        return SendBatchResult(sent: ids, skipped: skipped, ok: true)
    }

    /// POST the JSONL body; return the HTTP status.
    ///
    /// Returns **0** for a transport error or an initially-missing token. A
    /// FORCED (post-401) refresh that throws or yields nothing means the refresh
    /// grant is invalid or unavailable, so the user must re-authenticate — that
    /// is reported as **401 (unauthorized)**, not 0 (unreachable). The two
    /// statuses drive different UI: one says sign in again, the other says we
    /// could not reach the server, and conflating them sends the user to fix the
    /// wrong thing.
    private func postBatch(body: String, force: Bool) async -> Int {
        let accessToken: String?
        do {
            accessToken = try await token.accessToken(forceRefresh: force)
        } catch {
            return force ? 401 : 0
        }
        guard let accessToken, !accessToken.isEmpty else { return force ? 401 : 0 }

        var request = URLRequest(url: URL(string: url)!)
        request.httpMethod = "POST"
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        request.setValue(INGEST_CONTENT_TYPE, forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(body.utf8)

        do {
            let (_, response) = try await http.send(request)
            return response.statusCode
        } catch {
            return 0
        }
    }
}
