//
//  SignInView.swift
//  FulcraAttention (macOS App)
//
//  SwiftUI sign-in panel for the container app. Drives AuthManager.signIn()
//  and surfaces device-flow status (pending user code / signed in / error).
//

import SwiftUI
import Combine
import os

private let uiLog = Logger(subsystem: "com.fulcra.attention", category: "SignInView")

/// Observable state machine for the sign-in UI.
@MainActor
public final class SignInViewModel: ObservableObject {

    public enum Status: Equatable {
        case signedOut
        case pending(userCode: String, verificationURL: String)
        case signedIn
        case error(String)
    }

    @Published public private(set) var status: Status = .signedOut

    private let auth: AuthManager
    private var signInTask: Task<Void, Never>?

    public init(auth: AuthManager = AuthManager()) {
        self.auth = auth
        // Reflect any already-stored tokens on launch.
        if auth.currentTokens() != nil {
            self.status = .signedIn
        }
        // When a device code arrives mid-flow, surface the user code + URL.
        auth.onDeviceCode = { [weak self] device in
            Task { @MainActor in
                self?.status = .pending(
                    userCode: device.userCode,
                    verificationURL: device.verificationURIComplete
                )
            }
        }
    }

    public var isPending: Bool {
        if case .pending = status { return true }
        return false
    }

    public func signIn() {
        guard signInTask == nil else { return }
        uiLog.info("user tapped Sign in")
        signInTask = Task { [weak self] in
            guard let self else { return }
            do {
                _ = try await self.auth.signIn()
                self.status = .signedIn
            } catch is CancellationError {
                self.status = .signedOut
            } catch {
                uiLog.error("sign-in failed: \(error.localizedDescription, privacy: .public)")
                self.status = .error(error.localizedDescription)
            }
            self.signInTask = nil
        }
    }

    public func cancel() {
        signInTask?.cancel()
        signInTask = nil
        status = .signedOut
    }

    public func signOut() {
        try? auth.signOut()
        status = .signedOut
    }
}

public struct SignInView: View {
    @StateObject private var model: SignInViewModel

    @MainActor
    public init() {
        _model = StateObject(wrappedValue: SignInViewModel())
    }

    public init(model: SignInViewModel) {
        _model = StateObject(wrappedValue: model)
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Fulcra Account")
                .font(.headline)

            switch model.status {
            case .signedOut:
                Text("Sign in to capture your browsing attention into Fulcra.")
                    .foregroundStyle(.secondary)
                Button("Sign in to Fulcra") { model.signIn() }
                    .keyboardShortcut(.defaultAction)

            case let .pending(userCode, verificationURL):
                Text("Approve sign-in in the browser.")
                    .foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    Text("Code:")
                    Text(userCode)
                        .font(.system(.title3, design: .monospaced).bold())
                        .textSelection(.enabled)
                }
                Link("Open approval page", destination: URL(string: verificationURL) ?? URL(string: "https://fulcra.us.auth0.com")!)
                ProgressView().controlSize(.small)
                Button("Cancel") { model.cancel() }

            case .signedIn:
                Label("Signed in", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Button("Sign out") { model.signOut() }

            case let .error(message):
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                Button("Try again") { model.signIn() }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

#Preview {
    SignInView()
        .frame(width: 360)
}
