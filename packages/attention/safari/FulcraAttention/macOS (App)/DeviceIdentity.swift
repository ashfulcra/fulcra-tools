//
//  DeviceIdentity.swift
//  Fulcra Attention
//
//  An automatic, per-installation identity for THIS Safari install, folded into
//  every source_id so two devices never collide.
//
//  WHY THIS EXISTS. Until now the Safari path built its WireContext with
//  `identitySlug: ""`. The slug is hash input to source_id, so every Safari
//  install hashed identically: an iPhone and a Mac visiting the same URL in the
//  same second produced BYTE-IDENTICAL source_ids and server-side dedup dropped
//  one of them. Chrome never had this problem — its onboarding wizard refuses to
//  continue with a blank browser name, so its slug is always set. The attention
//  README names this property "the multi-browser distinctness guarantee", and
//  shipping Safari with an empty slug would have falsified it.
//
//  WHY NOT identifierForVendor, which is the obvious answer on iOS: it is
//  UIKit-only. This file is compiled into the macOS extension too, and the macOS
//  Safari extension has the exact same empty-slug defect. An IDFV-based fix
//  would have repaired the instance I happened to be looking at and left its
//  sibling broken on the other platform. A UUID persisted in the shared App
//  Group works identically on both, needs no UIKit, no entitlement, and no
//  platform #if.
//
//  WHY NOT A HUMAN LABEL, which is what Chrome uses: the label does two
//  different jobs, and only one of them is urgent. Distinctness (this file)
//  needs uniqueness and nothing else, and is permanently unfixable after the
//  fact because source_ids are already written. Legibility — telling your phone
//  from your laptop when reading the data back — needs a readable name, and can
//  arrive whenever. So this deliberately supplies ONLY the slug and leaves
//  `identityLabel` nil, which is why no `machine:` tag is minted here. The wire
//  layer already models these as two separate fields; this is that seam being
//  used as intended, not worked around.
//
//  THE FAILURE MODE, STATED PLAINLY. This id lives in the App Group container,
//  so deleting the app (or its data) mints a new one, and the same device then
//  appears as two. That OVER-COUNTS devices; it never MERGES them. The asymmetry
//  is the whole argument for this design: an over-count is visible and
//  recoverable at query time, whereas a merge silently drops one device's data
//  and cannot be undone. Any identity scheme here must be injective — distinct
//  devices must never map to the same slug — and this one is, because a fresh
//  random UUID never collides with an existing one.

import Foundation

public enum DeviceIdentity {

    /// Key in the shared App Group defaults. Not the keychain: this is not a
    /// secret, it is an opaque per-install label, and the keychain's
    /// access-group requirements would make it fail closed in exactly the
    /// contexts (the extension) that need to read it.
    public static let defaultsKey = "deviceIdentitySlug"

    /// The stable slug for this installation, minting one on first use.
    ///
    /// Every caller must get the SAME value forever: the slug is hash input to
    /// source_id, so a value that changed per call would mint a brand-new
    /// source_id for every batch. Nothing would dedup, and one device would
    /// look like thousands — a louder failure than the collision this replaces,
    /// but a failure all the same.
    public static func slug(defaults: UserDefaults = Sharing.sharedDefaults()) -> String {
        if let existing = defaults.string(forKey: defaultsKey), !isBlank(existing) {
            return existing
        }
        let minted = UUID().uuidString.lowercased()
        defaults.set(minted, forKey: defaultsKey)
        // Read back rather than returning `minted` directly. The app and the
        // extension are separate processes and can both reach a first run at
        // once; whoever wrote last is the value that persists, and adopting it
        // makes the two processes converge instead of disagreeing for the
        // lifetime of the install. This narrows the race but does not close it
        // — and when it does lose, the outcome is a second id for one device,
        // which is the over-count case above, never a merge.
        if let settled = defaults.string(forKey: defaultsKey), !isBlank(settled) {
            return settled
        }
        return minted
    }

    /// A stored value of "" or whitespace is treated as ABSENT, not as a valid
    /// slug. This is not defensive padding: the empty string is precisely the
    /// value that caused the collision this file fixes, so silently accepting
    /// one out of storage would reintroduce the bug through the back door and
    /// look like a working identity while doing it.
    private static func isBlank(_ value: String) -> Bool {
        value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}
