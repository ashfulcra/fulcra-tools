//
//  Sharing.swift
//  FulcraAttention (macOS App)
//
//  Central identifiers for app<->extension sharing. These MUST match the
//  entitlements on BOTH targets (app + extension):
//    - com.apple.security.application-groups   → App Group (shared, UNENCRYPTED
//      UserDefaults/container) for the NON-secret resolved {definitionId,
//      tagIds} cache.
//    - keychain-access-groups                  → shared Keychain group for the
//      access TOKEN (the only place secrets are shared).
//
//  These capabilities require a one-time Apple Developer portal registration:
//  with Automatic signing, Xcode registers the App Group + keychain group and
//  regenerates the provisioning profiles on the FIRST GUI build after the
//  capability is enabled. Until then, headless `xcodebuild` signing fails — so
//  this layer is wired but the runtime sharing stays OPT-IN (callers pass these
//  ids explicitly) to avoid breaking the current signed build.
//

import Foundation

public enum Sharing {
    /// App Group identifier — shared UserDefaults suite + container. Used for
    /// the non-secret resolved-id cache: `UserDefaults(suiteName: Sharing.appGroup)`.
    public static let appGroup = "group.com.fulcra.attention"

    /// The Apple Developer team identifier (DEVELOPMENT_TEAM = CWH48N2H7F). This
    /// is the value `$(AppIdentifierPrefix)`/`$(TeamIdentifierPrefix)` expands to
    /// (minus the trailing dot) in the entitlements files at build time.
    public static let teamIdentifierPrefix = "CWH48N2H7F"

    /// The bare keychain access group (matches the entitlement's
    /// `$(AppIdentifierPrefix)com.fulcra.attention.shared` minus the prefix).
    public static let keychainAccessGroupSuffix = "com.fulcra.attention.shared"

    /// The FULL keychain access group for `kSecAttrAccessGroup` at runtime —
    /// "<TeamID>.com.fulcra.attention.shared". Must exactly match an entry in
    /// both targets' `keychain-access-groups` entitlement. Pass to
    /// `KeychainStore(accessGroup:)` once the entitlement is registered.
    public static let keychainAccessGroup =
        "\(teamIdentifierPrefix).\(keychainAccessGroupSuffix)"

    /// The shared UserDefaults suite for the resolved-id cache, or `.standard`
    /// if the App Group suite can't be opened (entitlement not yet registered).
    /// Lets the ingest/bridge layer construct the cache without crashing before
    /// the capability is live.
    public static func sharedDefaults() -> UserDefaults {
        UserDefaults(suiteName: appGroup) ?? .standard
    }
}
