//
//  NativeBridge.swift
//  Shared (Extension)
//
//  Step 7 of the native track: the extension→native message bridge.
//  (docs/proposals/2026-06-04-relayless-and-mobile-safari-attention.md)
//
//  The JS side captures visits (capture/visibility.ts) and hands batches to the
//  native process, which owns auth, the token, and ingest — the architecture the
//  2026-06-07 addendum settled on after the Origin blocker made direct
//  extension→cloud posting impossible.
//
//  WHY THE LOGIC IS NOT IN SafariWebExtensionHandler: `beginRequest` takes an
//  `NSExtensionContext` and can only run inside a real extension host, so
//  anything written there is untestable by construction. The handler is a thin
//  adapter; everything decidable lives here behind injectable seams and is
//  covered by the SwiftPM harness.
//
//  THE PROTOCOL IS DEFINED HERE FIRST. As of this commit the TypeScript side
//  does not send native messages at all — verified, not assumed — so this file
//  is one half of a contract nothing yet speaks. Both halves must agree on the
//  SNAKE_CASE wire keys below, which are the keys `AttentionEvent` already uses
//  in chrome/src/types.ts. They are not the Swift property names.
//

import Foundation
import os

private let bridgeLog = Logger(subsystem: "com.fulcra.attention", category: "bridge")

// MARK: - Wire protocol

/// Message kinds the extension may send. Unknown kinds are refused rather than
/// ignored: a silently-dropped message looks identical to a delivered one from
/// the JS side, which is the failure this project keeps paying for.
public enum BridgeRequest: String {
    case ingest
    case authState = "auth_state"
}

public enum BridgeKey {
    public static let type = "type"
    public static let events = "events"
    public static let ok = "ok"
    public static let error = "error"
    public static let needsSignIn = "needs_sign_in"
    public static let sent = "sent"
    public static let skipped = "skipped"
    public static let signedIn = "signed_in"
}

/// Why a batch could not be decoded. Carried back to the JS side so a bad event
/// is a REPORTED failure, never a quiet omission.
public struct BridgeDecodeError: Error, LocalizedError {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

// MARK: - Decoding

public enum BridgeDecode {
    /// Decode one AttentionEvent from the JS wire shape.
    ///
    /// `start_time`, `end_time` and `client` are REQUIRED; everything else is
    /// genuinely nullable in the TS type. A missing required field throws rather
    /// than defaulting, because a defaulted timestamp would produce a
    /// well-formed record describing a moment that never happened — worse than
    /// a rejected batch, which at least tells someone.
    public static func event(from raw: [String: Any]) throws -> AttentionEvent {
        func optionalString(_ key: String) -> String? {
            // JSON null arrives as NSNull, which is NOT nil in Swift.
            guard let v = raw[key], !(v is NSNull) else { return nil }
            return v as? String
        }
        func requiredString(_ key: String) throws -> String {
            guard let v = optionalString(key), !v.isEmpty else {
                throw BridgeDecodeError("event is missing required field '\(key)'")
            }
            return v
        }
        return AttentionEvent(
            url: optionalString("url"),
            title: optionalString("title"),
            ogDescription: optionalString("og_description"),
            faviconURL: optionalString("favicon_url"),
            category: optionalString("category"),
            chromeIdentity: optionalString("chrome_identity"),
            ogType: optionalString("og_type"),
            lang: optionalString("lang"),
            startTime: try requiredString("start_time"),
            endTime: try requiredString("end_time"),
            client: try requiredString("client")
        )
    }

    /// Decode a whole batch. ALL-OR-NOTHING on purpose: one undecodable event
    /// fails the batch so the JS side keeps its outbox and retries, mirroring
    /// what `RelaylessSender.sendBatch` does with an unbuildable event. Decoding
    /// what we can and quietly discarding the rest would report success over a
    /// batch that lost a record (PR 601 r1).
    public static func batch(from raw: Any?) throws -> [[String: Any]] {
        guard let list = raw as? [[String: Any]] else {
            throw BridgeDecodeError("'\(BridgeKey.events)' must be an array of objects")
        }
        return list
    }
}

// MARK: - The bridge

/// Handles one decoded native message. Injectable so the SwiftPM harness can
/// exercise it without an extension host, a keychain, or the network.
public struct NativeBridge: Sendable {
    /// Returns the sender to ingest with, or nil when the extension cannot see
    /// a token. Separate from `TokenProvider` so "we have no access to the
    /// shared keychain yet" is representable — see the note on the App Group
    /// requirement below.
    public typealias SenderResolver = @Sendable () async -> RelaylessSender?
    public typealias ContextResolver = @Sendable () async throws -> WireContext

    private let sender: SenderResolver
    private let context: ContextResolver

    public init(sender: @escaping SenderResolver, context: @escaping ContextResolver) {
        self.sender = sender
        self.context = context
    }

    /// Handle a message from `browser.runtime.sendNativeMessage`.
    ///
    /// Never throws: the JS side gets a dictionary describing what happened,
    /// because an exception crossing the extension boundary would surface to the
    /// page as an opaque failure with no way to distinguish "sign in again" from
    /// "that batch was malformed".
    public func handle(_ message: [String: Any]) async -> [String: Any] {
        guard let rawType = message[BridgeKey.type] as? String,
              let kind = BridgeRequest(rawValue: rawType) else {
            let got = (message[BridgeKey.type] as? String) ?? "<missing>"
            bridgeLog.error("bridge: refusing unknown message type \(got, privacy: .public)")
            return [BridgeKey.ok: false,
                    BridgeKey.error: "unknown message type '\(got)'"]
        }

        switch kind {
        case .authState:
            let signedIn = await sender() != nil
            return [BridgeKey.ok: true, BridgeKey.signedIn: signedIn]

        case .ingest:
            return await ingest(message)
        }
    }

    private func ingest(_ message: [String: Any]) async -> [String: Any] {
        let rawEvents: [[String: Any]]
        do {
            rawEvents = try BridgeDecode.batch(from: message[BridgeKey.events])
        } catch {
            return [BridgeKey.ok: false, BridgeKey.error: "\(error)"]
        }

        var events: [AttentionEvent] = []
        events.reserveCapacity(rawEvents.count)
        for raw in rawEvents {
            do {
                events.append(try BridgeDecode.event(from: raw))
            } catch {
                // Fail the batch, do not partially accept it. The JS side still
                // holds these events; reporting success over a dropped one is
                // how they would be lost for good.
                bridgeLog.error("bridge: batch rejected: \(String(describing: error), privacy: .public)")
                return [BridgeKey.ok: false, BridgeKey.error: "\(error)"]
            }
        }

        guard let sender = await sender() else {
            // NOT an error state the user can act on by retrying — the native
            // side has no token. Flagged distinctly so the JS side can prompt a
            // sign-in instead of silently backing off.
            return [BridgeKey.ok: false, BridgeKey.needsSignIn: true,
                    BridgeKey.error: "not signed in, or the shared keychain is unavailable"]
        }

        do {
            let wireContext = try await context()
            let result = try await sender.sendBatch(events, context: wireContext)
            return [BridgeKey.ok: result.ok,
                    BridgeKey.sent: result.sent.count,
                    BridgeKey.skipped: result.skipped.count]
        } catch is UnauthorizedError {
            return [BridgeKey.ok: false, BridgeKey.needsSignIn: true,
                    BridgeKey.error: "the stored credentials were rejected"]
        } catch {
            // Includes the wire-build throw that PR 601 r2 introduced. It must
            // reach the JS side as a failure so the outbox is retained.
            return [BridgeKey.ok: false, BridgeKey.error: "\(error)"]
        }
    }
}
