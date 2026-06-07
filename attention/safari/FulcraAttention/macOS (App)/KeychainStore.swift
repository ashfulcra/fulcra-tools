//
//  KeychainStore.swift
//  FulcraAttention (macOS App)
//
//  Thin wrapper over the Security framework for storing the token JSON blob.
//  App-scoped for THIS milestone.
//  TODO: keychain access group for app<->extension sharing (later milestone)
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

    public init() {}

    /// Write (insert or update) `data` under (service, account).
    public func write(_ data: Data, service: String, account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]

        let attributes: [String: Any] = [
            kSecValueData as String: data,
            // Available after first unlock; not migrated to other devices.
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]

        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecSuccess {
            keychainLog.debug("updated keychain item service=\(service, privacy: .public)")
            return
        }
        if updateStatus == errSecItemNotFound {
            var insert = query
            insert[kSecValueData as String] = data
            insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
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
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

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
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            keychainLog.error("keychain delete failed status=\(status)")
            throw KeychainError.unexpectedStatus(status)
        }
    }
}
