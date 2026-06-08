//
//  KeychainStore.swift
//  FulcraAttention (macOS App)
//
//  Thin wrapper over the Security framework for storing the token JSON blob.
//
//  Optionally scopes items to a shared keychain access group so the Safari
//  Web Extension's handler can read the token the app stored (app<->extension
//  sharing). The access group is OPT-IN via `init(accessGroup:)`; the default
//  (nil) preserves the original app-private behavior, so existing call sites and
//  the current signed build are unaffected until the keychain-access-groups
//  entitlement is registered (Ash's one-time Xcode capability step). Pass
//  `Sharing.keychainAccessGroup` once the entitlement is live.
//

import Foundation
import Security
import os

private nonisolated let keychainLog = Logger(subsystem: "com.fulcra.attention", category: "KeychainStore")

public enum KeychainError: LocalizedError {
    case unexpectedStatus(OSStatus)

    public var errorDescription: String? {
        switch self {
        case let .unexpectedStatus(status):
            let message = SecCopyErrorMessageString(status, nil) as String? ?? "unknown"
            return "Keychain error \(status): \(message)"
        }
    }
}

/// Generic-password Keychain store keyed by (service, account).
public nonisolated struct KeychainStore {

    /// When non-nil, every query is scoped to this keychain access group so the
    /// app and its Safari extension share the item. Must EXACTLY match an entry
    /// in both targets' `keychain-access-groups` entitlement, including the
    /// team prefix (e.g. "CWH48N2H7F.com.fulcra.attention.shared"); use
    /// `Sharing.keychainAccessGroup`. Nil → app-private (original behavior).
    public let accessGroup: String?

    public init(accessGroup: String? = nil) {
        self.accessGroup = accessGroup
    }

    /// Base (service, account[, access-group]) query shared by all operations.
    private func baseQuery(service: String, account: String) -> [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let accessGroup {
            q[kSecAttrAccessGroup as String] = accessGroup
            // On macOS, access groups only apply to data-protection or
            // synchronizable keychain queries. Keep app-private queries on the
            // original default keychain by enabling this only for shared items.
            q[kSecUseDataProtectionKeychain as String] = true
        }
        return q
    }

    /// Write (insert or update) `data` under (service, account).
    public func write(_ data: Data, service: String, account: String) throws {
        let query = baseQuery(service: service, account: account)

        let attributes: [String: Any] = [
            kSecValueData as String: data,
            // Available after first unlock; not migrated to other devices.
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]

        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess {
            keychainLog.debug("updated keychain item service=\(service, privacy: .public)")
            return
        }
        if updateStatus == errSecItemNotFound {
            var insert = query
            insert[kSecValueData as String] = data
            insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            let addStatus = SecItemAdd(insert as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                keychainLog.error("keychain add failed status=\(addStatus)")
                throw KeychainError.unexpectedStatus(addStatus)
            }
            keychainLog.debug("inserted keychain item service=\(service, privacy: .public)")
            return
        }
        keychainLog.error("keychain update failed status=\(updateStatus)")
        throw KeychainError.unexpectedStatus(updateStatus)
    }

    /// Read the data stored under (service, account). Returns nil if absent.
    public func read(service: String, account: String) throws -> Data? {
        var query = baseQuery(service: service, account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess:
            return item as? Data
        case errSecItemNotFound:
            return nil
        default:
            keychainLog.error("keychain read failed status=\(status)")
            throw KeychainError.unexpectedStatus(status)
        }
    }

    /// Delete the item under (service, account). No-op if absent.
    public func delete(service: String, account: String) throws {
        let query = baseQuery(service: service, account: account)
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            keychainLog.error("keychain delete failed status=\(status)")
            throw KeychainError.unexpectedStatus(status)
        }
    }
}
