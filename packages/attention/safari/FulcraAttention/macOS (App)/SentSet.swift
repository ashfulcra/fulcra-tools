//
//  SentSet.swift
//  FulcraAttention
//
//  Swift port of chrome/src/relayless/sentSet.ts — the bounded record of
//  attention source_ids the relayless sender has already POSTed.
//
//  Fulcra dedupes server-side on source_id, so this is a client-side
//  optimisation (avoid re-POSTing every flush) rather than a correctness
//  guarantee — but it also implements the daemon's "claim-then-record"
//  posture, so a single flush never double-sends within itself.
//
//  Bounded: insertion order is preserved and the OLDEST ids are dropped once
//  the cap is exceeded, so the set cannot grow without limit. Worst case of
//  dropping an old id is one redundant (server-deduped) re-POST.
//
//  Storage note: the default store is the SHARED App Group suite, because the
//  app and the Safari extension are separate processes that must not each keep
//  their own idea of what has been sent. `Sharing.sharedDefaults()` falls back
//  to `.standard` until the App Group entitlement is registered, so this is
//  safe to construct before that one-time capability step.
//

import Foundation

/// Persisted list of already-sent source_ids. Mirrors the TS `StorageArea`
/// seam so tests can supply an in-memory double.
///
/// The contract is a LIST, not a Set: insertion order IS the eviction order.
public protocol SentIdStore: Sendable {
    func load() -> [String]
    func save(_ ids: [String])
    func removeAll()
}

/// Default store: UserDefaults, matching `UserDefaultsResolvedCache`.
///
/// Defaults to `.standard`, NOT the App Group suite — sharing stays OPT-IN
/// until the capability is registered in the Apple Developer portal, the same
/// posture Sharing.swift and the resolved-id cache already take. Once that
/// one-time step lands, pass `Sharing.sharedDefaults()` here so the app and the
/// extension stop keeping separate sets.
///
/// Consequence until then, stated plainly: two processes each keep their own
/// record of what they sent, so the same event can be POSTed twice. That is the
/// documented worst case for this set and it is not a data bug — Fulcra dedupes
/// server-side on source_id, so the cost is a redundant request, not a
/// duplicate record.
public final class UserDefaultsSentIdStore: SentIdStore, @unchecked Sendable {
    /// Matches the TS `SENT_KEY` so a future shared-storage migration can find it.
    public static let key = "relaylessSentIds"

    private let defaults: UserDefaults
    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public func load() -> [String] {
        defaults.stringArray(forKey: Self.key) ?? []
    }

    public func save(_ ids: [String]) {
        defaults.set(ids, forKey: Self.key)
    }

    public func removeAll() {
        defaults.removeObject(forKey: Self.key)
    }
}

/// Mirrors `SENT_SET_CAP` in sentSet.ts. Sized well above a realistic in-flight
/// backlog.
public let SENT_SET_CAP = 10_000

/// A bounded, insertion-ordered record of sent source_ids.
public struct SentSet: Sendable {
    private let store: SentIdStore
    private let cap: Int

    public init(store: SentIdStore = UserDefaultsSentIdStore(), cap: Int = SENT_SET_CAP) {
        self.store = store
        self.cap = cap
    }

    /// True if `id` has already been recorded as sent.
    public func has(_ id: String) -> Bool {
        store.load().contains(id)
    }

    /// Load the whole set ONCE for O(1) membership checks during a flush.
    ///
    /// Returns the ordered ids alongside the Set: the order is the persisted
    /// eviction order, and `addMany` needs it to trim the oldest correctly. The
    /// TS version can pass a bare `Set` here because JS Sets preserve insertion
    /// order; Swift's `Set` does NOT, so returning only a Set would silently
    /// randomise which ids get evicted at the cap.
    public func snapshot() -> (ordered: [String], membership: Set<String>) {
        let ordered = store.load()
        return (ordered, Set(ordered))
    }

    /// Record ids as sent: de-duped, insertion order preserved, oldest dropped
    /// once over the cap. A no-op (no read, NO write) when `ids` is empty.
    ///
    /// Callers holding a `snapshot()` from this same flush pass it as `known` to
    /// skip the read, so a whole flush costs one read and one write.
    public func addMany(_ ids: [String], known: (ordered: [String], membership: Set<String>)? = nil) {
        guard !ids.isEmpty else { return }
        var current = known?.ordered ?? store.load()
        var seen = known?.membership ?? Set(current)
        for id in ids where !seen.contains(id) {
            current.append(id)
            seen.insert(id)
        }
        if current.count > cap {
            current.removeFirst(current.count - cap)
        }
        store.save(current)
    }

    /// Convenience alias matching the TS `add`.
    public func add(_ ids: [String]) { addMany(ids) }

    /// Number of recorded ids (diagnostics/tests).
    public func size() -> Int { store.load().count }

    /// Drop the entire set. Used on sign-out / account switch: source_id omits
    /// the account and definition, so a re-queued id from a prior account must
    /// not be skipped against the new account's definition.
    public func clear() { store.removeAll() }
}
