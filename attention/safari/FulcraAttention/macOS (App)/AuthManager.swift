//
//  AuthManager.swift
//  FulcraAttention (macOS App)
//
//  Native Auth0 OAuth 2.0 Device Authorization Grant implementation.
//
//  Why native (not the Safari extension): Safari refuses to strip the request
//  `Origin` header on extension-originated fetches, so Auth0 rejects the
//  device-code call with HTTP 403. A native `URLSession` request carries no
//  browser `Origin`, so Auth0 returns HTTP 200. This file runs the full device
//  flow in the app container and persists tokens to the Keychain.
//
//  See: docs/proposals/2026-06-04-relayless-and-mobile-safari-attention.md
//

import Foundation
import os

#if os(macOS)
import AppKit
#endif

// MARK: - Logging

/// Structured logging (per project CLAUDE.md: levels + component/operation context).
/// Subsystem is the app bundle id; category identifies this component.
private nonisolated let authLog = Logger(subsystem: "com.fulcra.attention", category: "AuthManager")

// MARK: - Configuration

/// Auth0 device-flow configuration for the Fulcra public client.
public nonisolated struct AuthConfig: Sendable {
    public let domain: String
    public let clientID: String
    public let audience: String
    public let scope: String

    /// The production Fulcra Auth0 tenant + public client used by the CLI.
    public static let fulcra = AuthConfig(
        domain: "fulcra.us.auth0.com",
        clientID: "48p3VbMnr5kMuJAUe9gJ9vjmdWLdnqZt",
        audience: "https://api.fulcradynamics.com/",
        scope: "openid profile name email offline_access"
    )

    var deviceCodeURL: URL { URL(string: "https://\(domain)/oauth/device/code")! }
    var tokenURL: URL { URL(string: "https://\(domain)/oauth/token")! }
}

// MARK: - Models

/// Response from `POST /oauth/device/code`.
public nonisolated struct DeviceCodeResponse: Codable, Sendable {
    public let deviceCode: String
    public let userCode: String
    public let verificationURI: String?
    public let verificationURIComplete: String
    public let expiresIn: Int
    public let interval: Int

    enum CodingKeys: String, CodingKey {
        case deviceCode = "device_code"
        case userCode = "user_code"
        case verificationURI = "verification_uri"
        case verificationURIComplete = "verification_uri_complete"
        case expiresIn = "expires_in"
        case interval
    }
}

/// Successful token response from `POST /oauth/token`.
public nonisolated struct TokenResponse: Codable, Sendable {
    public let accessToken: String
    /// Auth0 only returns a refresh token when `offline_access` is in scope and
    /// rotation/issuance is enabled; treat it as optional and preserve any
    /// previously stored refresh token across refreshes when absent.
    public let refreshToken: String?
    public let idToken: String?
    public let tokenType: String?
    public let expiresIn: Int?

    enum CodingKeys: String, CodingKey {
        case accessToken = "access_token"
        case refreshToken = "refresh_token"
        case idToken = "id_token"
        case tokenType = "token_type"
        case expiresIn = "expires_in"
    }
}

/// Tokens as persisted to the Keychain (JSON-encoded).
public nonisolated struct StoredTokens: Codable, Sendable {
    public var accessToken: String
    public var refreshToken: String?
    public var idToken: String?
    public var tokenType: String
    /// Absolute expiry instant (computed from `expires_in` at fetch time).
    public var expiresAt: Date?

    public init(from token: TokenResponse, fetchedAt: Date = Date()) {
        self.accessToken = token.accessToken
        self.refreshToken = token.refreshToken
        self.idToken = token.idToken
        self.tokenType = token.tokenType ?? "Bearer"
        if let expiresIn = token.expiresIn {
            self.expiresAt = fetchedAt.addingTimeInterval(TimeInterval(expiresIn))
        } else {
            self.expiresAt = nil
        }
    }

    /// Whether the access token is expired (with a 60s safety margin).
    public var isExpired: Bool {
        guard let expiresAt else { return false }
        return Date() >= expiresAt.addingTimeInterval(-60)
    }
}

/// Auth0 OAuth error payload (used for both 4xx token errors and device errors).
private nonisolated struct OAuthError: Codable {
    let error: String
    let errorDescription: String?

    enum CodingKeys: String, CodingKey {
        case error
        case errorDescription = "error_description"
    }
}

// MARK: - Errors

public enum AuthError: LocalizedError {
    case http(status: Int, body: String)
    case oauth(code: String, description: String?)
    case deviceCodeExpired
    case accessDenied
    case decoding(String)
    case noRefreshToken
    case cancelled

    public var errorDescription: String? {
        switch self {
        case let .http(status, body):
            return "HTTP \(status): \(body)"
        case let .oauth(code, description):
            return description ?? code
        case .deviceCodeExpired:
            return "The sign-in code expired before it was approved. Please try again."
        case .accessDenied:
            return "Sign-in was denied."
        case let .decoding(detail):
            return "Could not parse the server response: \(detail)"
        case .noRefreshToken:
            return "No refresh token is available; please sign in again."
        case .cancelled:
            return "Sign-in was cancelled."
        }
    }
}

// MARK: - AuthManager

/// Drives the Auth0 device authorization grant and persists tokens to the Keychain.
///
/// Usage: call `signIn()`; while pending, `onDeviceCode` fires with the user
/// code + verification URL so the UI can display them.
public final nonisolated class AuthManager: @unchecked Sendable {

    public let config: AuthConfig
    private let session: URLSession
    private let keychain: KeychainStore

    /// Keychain service under which the token JSON blob is stored.
    public static let keychainService = "com.fulcra.attention.tokens"
    private static let keychainAccount = "default"

    /// Fired (on an arbitrary queue) once a device code is obtained, so the UI
    /// can show the `user_code` and prompt the user to approve in the browser.
    public var onDeviceCode: ((DeviceCodeResponse) -> Void)?

    public nonisolated init(
        config: AuthConfig = .fulcra,
        session: URLSession = .shared,
        keychain: KeychainStore = KeychainStore()
    ) {
        self.config = config
        self.session = session
        self.keychain = keychain
    }

    // MARK: Public API

    /// Full sign-in: request a device code, open the verification URL in the
    /// browser, poll for tokens, and persist them to the Keychain on success.
    @discardableResult
    public func signIn() async throws -> StoredTokens {
        authLog.info("signIn started")
        let device = try await requestDeviceCode()
        authLog.info("device code obtained: userCode=\(device.userCode, privacy: .public) interval=\(device.interval)s expiresIn=\(device.expiresIn)s")

        onDeviceCode?(device)
        openInBrowser(device.verificationURIComplete)

        let token = try await pollForToken(deviceCode: device.deviceCode, interval: device.interval)
        let stored = StoredTokens(from: token)
        try persist(stored)
        authLog.info("signIn succeeded; tokens persisted to Keychain")
        return stored
    }

    /// `POST /oauth/device/code` (form: client_id, audience, scope).
    public func requestDeviceCode() async throws -> DeviceCodeResponse {
        let body = formBody([
            "client_id": config.clientID,
            "audience": config.audience,
            "scope": config.scope,
        ])
        authLog.debug("requestDeviceCode POST \(self.config.deviceCodeURL.absoluteString, privacy: .public)")
        let (data, response) = try await postForm(url: config.deviceCodeURL, body: body)
        try Self.ensureSuccess(data: data, response: response)
        do {
            return try JSONDecoder().decode(DeviceCodeResponse.self, from: data)
        } catch {
            authLog.error("requestDeviceCode decode failed: \(error.localizedDescription, privacy: .public)")
            throw AuthError.decoding(error.localizedDescription)
        }
    }

    /// Poll `POST /oauth/token` (grant_type device_code) until the user approves.
    ///
    /// - Handles `authorization_pending` (wait `interval`) and `slow_down`
    ///   (increase interval by 5s, per RFC 8628).
    /// - Stops with an error on `expired_token` / `access_denied`.
    public func pollForToken(deviceCode: String, interval: Int) async throws -> TokenResponse {
        var pollInterval = max(interval, 1)
        authLog.info("pollForToken started; interval=\(pollInterval)s")

        while true {
            try Task.checkCancellation()

            let body = formBody([
                "client_id": config.clientID,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": deviceCode,
            ])
            let (data, response) = try await postForm(url: config.tokenURL, body: body)
            let status = (response as? HTTPURLResponse)?.statusCode ?? -1

            if (200..<300).contains(status) {
                do {
                    let token = try JSONDecoder().decode(TokenResponse.self, from: data)
                    authLog.info("pollForToken succeeded (HTTP \(status))")
                    return token
                } catch {
                    authLog.error("pollForToken decode failed: \(error.localizedDescription, privacy: .public)")
                    throw AuthError.decoding(error.localizedDescription)
                }
            }

            // Non-2xx: expect an OAuth error code that drives the state machine.
            guard let oauthError = try? JSONDecoder().decode(OAuthError.self, from: data) else {
                let bodyStr = String(data: data, encoding: .utf8) ?? "<binary>"
                authLog.error("pollForToken HTTP \(status) with non-OAuth body: \(bodyStr, privacy: .public)")
                throw AuthError.http(status: status, body: bodyStr)
            }

            switch oauthError.error {
            case "authorization_pending":
                authLog.debug("authorization_pending; sleeping \(pollInterval)s")
                try await sleep(seconds: pollInterval)
            case "slow_down":
                pollInterval += 5
                authLog.debug("slow_down; new interval \(pollInterval)s")
                try await sleep(seconds: pollInterval)
            case "expired_token":
                authLog.error("device code expired")
                throw AuthError.deviceCodeExpired
            case "access_denied":
                authLog.error("access denied by user")
                throw AuthError.accessDenied
            default:
                authLog.error("pollForToken oauth error: \(oauthError.error, privacy: .public)")
                throw AuthError.oauth(code: oauthError.error, description: oauthError.errorDescription)
            }
        }
    }

    /// `POST /oauth/token` with grant_type `refresh_token`.
    ///
    /// Persists the refreshed tokens to the Keychain. If Auth0 does not return a
    /// new refresh token, the supplied one is preserved.
    @discardableResult
    public func refresh(refreshToken: String) async throws -> StoredTokens {
        authLog.info("refresh started")
        let body = formBody([
            "client_id": config.clientID,
            "grant_type": "refresh_token",
            "refresh_token": refreshToken,
        ])
        let (data, response) = try await postForm(url: config.tokenURL, body: body)
        try Self.ensureSuccess(data: data, response: response)

        let token: TokenResponse
        do {
            token = try JSONDecoder().decode(TokenResponse.self, from: data)
        } catch {
            authLog.error("refresh decode failed: \(error.localizedDescription, privacy: .public)")
            throw AuthError.decoding(error.localizedDescription)
        }

        var stored = StoredTokens(from: token)
        // Preserve the prior refresh token if Auth0 didn't rotate/return one.
        if stored.refreshToken == nil {
            stored.refreshToken = refreshToken
        }
        try persist(stored)
        authLog.info("refresh succeeded; tokens persisted")
        return stored
    }

    /// Read the currently stored tokens, if any.
    public func currentTokens() -> StoredTokens? {
        guard let data = (try? keychain.read(
            service: Self.keychainService,
            account: Self.keychainAccount
        )) ?? nil else {
            return nil
        }
        return try? JSONDecoder().decode(StoredTokens.self, from: data)
    }

    /// Remove stored tokens (sign out).
    public func signOut() throws {
        authLog.info("signOut; clearing Keychain entry")
        try keychain.delete(service: Self.keychainService, account: Self.keychainAccount)
    }

    // MARK: Persistence

    private func persist(_ tokens: StoredTokens) throws {
        let data = try JSONEncoder().encode(tokens)
        // TODO: keychain access group for app<->extension sharing (later milestone)
        try keychain.write(
            data,
            service: Self.keychainService,
            account: Self.keychainAccount
        )
    }

    // MARK: HTTP helpers

    private func formBody(_ params: [String: String]) -> Data {
        var components = URLComponents()
        components.queryItems = params.map { URLQueryItem(name: $0.key, value: $0.value) }
        // `URLComponents.query` percent-encodes form fields appropriately.
        return (components.percentEncodedQuery ?? "").data(using: .utf8) ?? Data()
    }

    private func postForm(url: URL, body: Data) async throws -> (Data, URLResponse) {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = body
        // Native URLSession sends NO browser `Origin` header — this is the whole
        // reason auth lives here and not in the Safari extension.
        return try await session.data(for: request)
    }

    private static func ensureSuccess(data: Data, response: URLResponse) throws {
        let status = (response as? HTTPURLResponse)?.statusCode ?? -1
        guard (200..<300).contains(status) else {
            if let oauthError = try? JSONDecoder().decode(OAuthError.self, from: data) {
                throw AuthError.oauth(code: oauthError.error, description: oauthError.errorDescription)
            }
            let bodyStr = String(data: data, encoding: .utf8) ?? "<binary>"
            throw AuthError.http(status: status, body: bodyStr)
        }
    }

    private func sleep(seconds: Int) async throws {
        try await Task.sleep(nanoseconds: UInt64(seconds) * 1_000_000_000)
    }

    private func openInBrowser(_ urlString: String) {
        guard let url = URL(string: urlString) else {
            authLog.error("cannot open verification URL: invalid \(urlString, privacy: .public)")
            return
        }
        authLog.info("opening verification URL in browser")
        #if os(macOS)
        NSWorkspace.shared.open(url)
        #endif
    }
}
