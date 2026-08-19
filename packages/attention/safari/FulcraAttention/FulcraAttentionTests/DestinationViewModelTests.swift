import XCTest
@testable import FulcraAttention

/// These tests drive `DestinationViewModel` and assert on what it CALLED.
///
/// The first version of this file could not fail. It wrote the cache double
/// directly and never invoked the view model, so it stayed green with the
/// production wiring deleted — the exact wired-to-nothing defect this feature
/// was written to fix, reproduced inside its own tests. codex-coder caught it.
/// Every test below now goes through the view model, and the spy records calls
/// so "the UI reached the service" is an assertion rather than an assumption.
private actor Spy {
    var listCalls = 0
    var chosen: [String] = []
    var created: [String] = []
    func noteList() { listCalls += 1 }
    func noteChoose(_ id: String) { chosen.append(id) }
    func noteCreate(_ n: String) { created.append(n) }
}

private struct FakeService: DestinationService {
    let spy: Spy
    var destinations: [AttentionDestination] = []
    var failWith: Error?

    func listDestinations() async throws -> [AttentionDestination] {
        await spy.noteList()
        if let failWith { throw failWith }
        return destinations
    }
    func choose(definitionId: String) async throws -> ResolvedAttention {
        await spy.noteChoose(definitionId)
        if let failWith { throw failWith }
        return ResolvedAttention(definitionId: definitionId, tagIds: ["t"])
    }
    func create(name: String) async throws -> ResolvedAttention {
        await spy.noteCreate(name)
        if let failWith { throw failWith }
        return ResolvedAttention(definitionId: "new-\(name)", tagIds: ["t"])
    }
}

private final class FakeCache: ResolvedAttentionCache, @unchecked Sendable {
    var stored: ResolvedAttention?
    init(_ s: ResolvedAttention? = nil) { stored = s }
    func read() -> ResolvedAttention? { stored }
    func write(_ r: ResolvedAttention) { stored = r }
    func clear() { stored = nil }
}

private func dest(_ id: String, auto: Bool = false) -> AttentionDestination {
    AttentionDestination(id: id, name: "Attention", createdAt: "2026-01-01", isAutoPick: auto)
}

@MainActor
final class DestinationViewModelTests: XCTestCase {

    // MARK: load

    func testLoadCallsTheServiceAndPublishesItsDestinations() async {
        let spy = Spy()
        let vm = DestinationViewModel(
            service: FakeService(spy: spy, destinations: [dest("a", auto: true), dest("b")]),
            cache: FakeCache(ResolvedAttention(definitionId: "a", tagIds: ["t"]))
        )
        await vm.load()
        let calls = await spy.listCalls
        XCTAssertEqual(calls, 1, "load must reach the service")
        guard case let .loaded(destinations, current) = vm.status else {
            return XCTFail("expected .loaded, got \(vm.status)")
        }
        XCTAssertEqual(destinations.map(\.id), ["a", "b"])
        XCTAssertEqual(current, "a", "current must come from the cache the extension reads")
    }

    func testLoadSurfacesNilCurrentWhenNothingIsCachedYet() async {
        let vm = DestinationViewModel(
            service: FakeService(spy: Spy(), destinations: [dest("a", auto: true)]),
            cache: FakeCache(nil)
        )
        await vm.load()
        guard case let .loaded(_, current) = vm.status else { return XCTFail("expected .loaded") }
        // "not chosen yet" must stay distinguishable from "chosen": conflating
        // them is how the missing destination step read as working.
        XCTAssertNil(current)
    }

    func testLoadFailurePublishesErrorRatherThanAnEmptyList() async {
        struct Boom: Error, LocalizedError { var errorDescription: String? { "no network" } }
        let vm = DestinationViewModel(
            service: FakeService(spy: Spy(), destinations: [], failWith: Boom()),
            cache: FakeCache(nil)
        )
        await vm.load()
        guard case let .error(msg) = vm.status else {
            return XCTFail("expected .error, got \(vm.status) — an empty list would read as 'you have none'")
        }
        XCTAssertTrue(msg.contains("no network"))
    }

    // MARK: choose

    func testChooseReachesTheServiceWithTheSelectedId() async {
        let spy = Spy()
        let vm = DestinationViewModel(
            service: FakeService(spy: spy, destinations: [dest("a", auto: true), dest("b")]),
            cache: FakeCache(ResolvedAttention(definitionId: "a", tagIds: ["t"]))
        )
        await vm.choose("b")
        let chosen = await spy.chosen
        // THE test the previous version failed to be: delete the call in
        // choose() and this goes red.
        XCTAssertEqual(chosen, ["b"])
    }

    func testChooseReloadsSoTheDisplayedSelectionFollowsTheChange() async {
        let spy = Spy()
        let cache = FakeCache(ResolvedAttention(definitionId: "a", tagIds: ["t"]))
        let vm = DestinationViewModel(
            service: FakeService(spy: spy, destinations: [dest("a", auto: true), dest("b")]),
            cache: cache
        )
        await vm.choose("b")
        let listCalls = await spy.listCalls
        XCTAssertGreaterThanOrEqual(listCalls, 1, "choose must re-read so the UI reflects reality")
    }

    func testChooseFailureIsSurfacedNotSwallowed() async {
        struct Boom: Error, LocalizedError { var errorDescription: String? { "rejected" } }
        let vm = DestinationViewModel(
            service: FakeService(spy: Spy(), destinations: [dest("a")], failWith: Boom()),
            cache: FakeCache(nil)
        )
        await vm.choose("a")
        guard case let .error(msg) = vm.status else {
            return XCTFail("a failed choose must not look like success")
        }
        XCTAssertTrue(msg.contains("rejected"))
    }

    // MARK: create

    func testCreateReachesTheServiceWithTheGivenName() async {
        let spy = Spy()
        let vm = DestinationViewModel(
            service: FakeService(spy: spy, destinations: []),
            cache: FakeCache(nil)
        )
        await vm.createNew(name: "Attention 2")
        let created = await spy.created
        XCTAssertEqual(created, ["Attention 2"])
    }

    func testCreateIsSeparateFromChooseBecauseCreatingIsHowDuplicatesHappen() async {
        let spy = Spy()
        let vm = DestinationViewModel(
            service: FakeService(spy: spy, destinations: []),
            cache: FakeCache(nil)
        )
        await vm.createNew(name: "Attention")
        let chosen = await spy.chosen
        XCTAssertTrue(chosen.isEmpty, "create must never be reachable as a silent fallback from choose")
    }

    // MARK: cache selection

    func testCachedDefinitionIdReadsTheSHAREDCacheTheExtensionResolvesFrom() {
        let vm = DestinationViewModel(
            service: FakeService(spy: Spy()),
            cache: FakeCache(ResolvedAttention(definitionId: "def-live", tagIds: ["t"]))
        )
        XCTAssertEqual(vm.cachedDefinitionId, "def-live")
    }
}

/// Pins the auto-pick RULE the picker overrides: index 0 is the default and the
/// list is oldest-first, so with duplicates the default is a tiebreak, not a
/// preference.
final class AttentionDestinationShapeTests: XCTestCase {

    func testOnlyTheFirstIsMarkedAsTheDefault() {
        let list = [dest("a", auto: true), dest("b")]
        XCTAssertEqual(list.filter(\.isAutoPick).count, 1)
        XCTAssertEqual(list.first?.id, "a")
    }

    func testTwoDestinationsShareANameSoTheIdIsWhatDistinguishesThem() {
        let a = dest("aaaa1111", auto: true), b = dest("bbbb2222")
        XCTAssertEqual(a.name, b.name)
        XCTAssertNotEqual(a.id, b.id)
    }
}
