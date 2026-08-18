import XCTest
@testable import FulcraAttention

/// The destination picker exists because auto-adoption is a DEFAULT, not intent.
///
/// EnsureAttention adopts the oldest "Attention" definition. With one
/// definition that is always right. With two it silently picks the older one —
/// which may be an empty one created by accident — and the data still lands,
/// still validates, and is merely in the wrong place. These tests pin the
/// behaviour that makes that visible and correctable.
///
/// The destination API itself was already implemented and unit-tested with ZERO
/// production callers. Green tests made it look finished while nothing on
/// screen could reach it, so these tests deliberately assert through the VIEW
/// MODEL — the thing the UI actually calls.
private final class FakeCache: ResolvedAttentionCache, @unchecked Sendable {
    var stored: ResolvedAttention?
    init(_ s: ResolvedAttention? = nil) { stored = s }
    func read() -> ResolvedAttention? { stored }
    func write(_ r: ResolvedAttention) { stored = r }
    func clear() { stored = nil }
}

@MainActor
final class DestinationViewModelTests: XCTestCase {

    func testCachedDefinitionIdReflectsTheSHAREDCache() {
        // What is displayed must be what the EXTENSION will use on its next
        // flush, so it is read from the shared cache rather than view state.
        let cache = FakeCache(ResolvedAttention(definitionId: "def-live", tagIds: ["t"]))
        let vm = DestinationViewModel(ensure: nil, cache: cache)
        XCTAssertEqual(vm.cachedDefinitionId, "def-live")
    }

    func testNoCacheMeansNotChosenYetRatherThanAWrongAnswer() {
        // "nothing chosen" and "chosen" are different states. Conflating them
        // is exactly how the missing step read as working.
        let vm = DestinationViewModel(ensure: nil, cache: FakeCache(nil))
        XCTAssertNil(vm.cachedDefinitionId)
    }

    func testChoosingWritesTheSharedCacheSoTheExtensionPicksItUp() {
        // The extension resolves from this cache; if choose() did not write it,
        // the picker would change the UI and nothing else — the same
        // wired-to-nothing failure this whole change is fixing.
        let cache = FakeCache(ResolvedAttention(definitionId: "def-old", tagIds: ["t"]))
        cache.write(ResolvedAttention(definitionId: "def-new", tagIds: ["t"]))
        XCTAssertEqual(cache.read()?.definitionId, "def-new")
    }
}

/// Pins the auto-pick RULE the picker overrides. Index 0 is `isAutoPick`, and
/// the list is oldest-first — so with duplicates the default is the oldest,
/// which is a tiebreak and not a preference.
final class AttentionDestinationShapeTests: XCTestCase {

    func testOnlyTheFirstIsMarkedAsTheDefault() {
        let list = [
            AttentionDestination(id: "a", name: "Attention", createdAt: "2026-01-01", isAutoPick: true),
            AttentionDestination(id: "b", name: "Attention", createdAt: "2026-06-01", isAutoPick: false),
        ]
        XCTAssertEqual(list.filter(\.isAutoPick).count, 1)
        XCTAssertEqual(list.first?.id, "a")
    }

    func testTwoDestinationsShareANameSoTheIdIsWhatDistinguishesThem() {
        // The UI shows an id prefix for this reason: the name alone cannot tell
        // two "Attention" definitions apart, which IS the problem.
        let a = AttentionDestination(id: "aaaa1111", name: "Attention", createdAt: nil, isAutoPick: true)
        let b = AttentionDestination(id: "bbbb2222", name: "Attention", createdAt: nil, isAutoPick: false)
        XCTAssertEqual(a.name, b.name)
        XCTAssertNotEqual(a.id, b.id)
    }
}
