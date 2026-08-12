import XCTest
@testable import FulcraAttention

/// A private UserDefaults suite per test, so nothing here touches the real App
/// Group container and tests cannot leak state into each other.
private func freshDefaults(_ name: String = UUID().uuidString) -> UserDefaults {
    let d = UserDefaults(suiteName: name)!
    d.removePersistentDomain(forName: name)
    return d
}

final class DeviceIdentityTests: XCTestCase {

    // MARK: - The property the whole change exists for

    func testSlugIsNotEmpty() {
        // The empty string IS the bug. Every Safari install used to pass "",
        // which is why two devices hashed to the same source_id.
        XCTAssertFalse(DeviceIdentity.slug(defaults: freshDefaults()).isEmpty)
    }

    func testTwoInstallationsGetDifferentSlugs() {
        // Distinct installs must never share a slug — this is the injectivity
        // requirement. If this ever fails, two devices' records merge and one
        // device's data is silently dropped by server-side dedup.
        let a = DeviceIdentity.slug(defaults: freshDefaults())
        let b = DeviceIdentity.slug(defaults: freshDefaults())
        XCTAssertNotEqual(a, b)
    }

    // MARK: - Stability

    func testSlugIsStableAcrossCalls() {
        // A slug that changed per call would be a LOUDER failure than the one
        // being fixed: every batch would mint fresh source_ids, nothing would
        // dedup, and one device would look like thousands.
        let d = freshDefaults()
        XCTAssertEqual(DeviceIdentity.slug(defaults: d), DeviceIdentity.slug(defaults: d))
    }

    func testSlugSurvivesANewDefaultsHandleOnTheSameSuite() {
        // The app and the extension are separate processes reading the same App
        // Group container. Persisting only in memory would give them different
        // identities for one device.
        let name = UUID().uuidString
        defer { UserDefaults().removePersistentDomain(forName: name) }
        let first = DeviceIdentity.slug(defaults: freshDefaults(name))
        let reopened = UserDefaults(suiteName: name)!
        XCTAssertEqual(DeviceIdentity.slug(defaults: reopened), first)
    }

    func testItPersistsUnderTheDocumentedKey() {
        let d = freshDefaults()
        let slug = DeviceIdentity.slug(defaults: d)
        XCTAssertEqual(d.string(forKey: DeviceIdentity.defaultsKey), slug)
    }

    // MARK: - A stored blank must never be accepted

    func testAStoredEmptyStringIsTreatedAsAbsent() {
        // The back door into the original bug: "" round-tripping out of storage
        // and being used as a valid slug would restore the collision while
        // looking like a working identity.
        let d = freshDefaults()
        d.set("", forKey: DeviceIdentity.defaultsKey)
        XCTAssertFalse(DeviceIdentity.slug(defaults: d).isEmpty)
    }

    func testAStoredWhitespaceSlugIsTreatedAsAbsent() {
        let d = freshDefaults()
        d.set("   \n ", forKey: DeviceIdentity.defaultsKey)
        let slug = DeviceIdentity.slug(defaults: d)
        XCTAssertFalse(slug.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    func testANonStringStoredValueIsTreatedAsAbsent() {
        // string(forKey:) coerces some types and returns nil for others; either
        // way a junk value must not become this device's identity.
        let d = freshDefaults()
        d.set(["not": "a string"], forKey: DeviceIdentity.defaultsKey)
        XCTAssertFalse(DeviceIdentity.slug(defaults: d).isEmpty)
    }

    func testARepairedBlankIsWrittenBackSoItIsRepairedONCE() {
        let d = freshDefaults()
        d.set("", forKey: DeviceIdentity.defaultsKey)
        let first = DeviceIdentity.slug(defaults: d)
        XCTAssertEqual(DeviceIdentity.slug(defaults: d), first)
    }

    func testAnExistingGoodSlugIsNEVEROverwritten() {
        // Regenerating over a good value would orphan every source_id already
        // written under it.
        let d = freshDefaults()
        d.set("already-mine", forKey: DeviceIdentity.defaultsKey)
        XCTAssertEqual(DeviceIdentity.slug(defaults: d), "already-mine")
    }

    // MARK: - The end-to-end property, in terms of source_id

    func testTwoDevicesNoLongerCollideOnTheSameUrlAndSecond() throws {
        // This is the actual defect, expressed the way it was measured: before
        // this change both sides passed "" and produced identical source_ids.
        let urlKey = "https://example.com/a"
        let start = "2026-08-12T21:30:00.000Z"

        let deviceA = DeviceIdentity.slug(defaults: freshDefaults())
        let deviceB = DeviceIdentity.slug(defaults: freshDefaults())

        let sidA = try XCTUnwrap(Wire.sourceId(key: urlKey, startTimeISO: start, identitySlug: deviceA))
        let sidB = try XCTUnwrap(Wire.sourceId(key: urlKey, startTimeISO: start, identitySlug: deviceB))
        XCTAssertNotEqual(sidA, sidB, "two devices still fold to one source_id")

        // And the old behaviour, pinned so a regression to "" is visible as a
        // failing test rather than as silently missing data months later.
        let empty1 = try XCTUnwrap(Wire.sourceId(key: urlKey, startTimeISO: start, identitySlug: ""))
        let empty2 = try XCTUnwrap(Wire.sourceId(key: urlKey, startTimeISO: start, identitySlug: ""))
        XCTAssertEqual(empty1, empty2, "sanity: empty slugs DO collide")
        XCTAssertNotEqual(sidA, empty1)
    }

    func testTheSameDeviceStillDedupsItself() {
        // Distinctness must not come at the cost of idempotency: one device
        // re-sending the same visit must still produce one source_id.
        let d = freshDefaults()
        let s1 = Wire.sourceId(key: "https://example.com/a",
                               startTimeISO: "2026-08-12T21:30:00.000Z",
                               identitySlug: DeviceIdentity.slug(defaults: d))
        let s2 = Wire.sourceId(key: "https://example.com/a",
                               startTimeISO: "2026-08-12T21:30:00.000Z",
                               identitySlug: DeviceIdentity.slug(defaults: d))
        XCTAssertEqual(s1, s2)
    }
}
