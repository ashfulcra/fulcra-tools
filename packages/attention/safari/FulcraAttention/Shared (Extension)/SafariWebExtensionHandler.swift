//
//  SafariWebExtensionHandler.swift
//  Shared (Extension)
//
//  Created by Ash Kalb on 6/7/26.
//
//  Thin adapter only. Every decision lives in NativeBridge, which is testable
//  without an extension host; this file does the two things that genuinely
//  require the host — unwrap NSExtensionContext, and complete the request.
//  Logic added here instead would be untestable by construction.
//

import SafariServices
import os.log

class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {

    /// Built per request rather than stored, so a token that changes between
    /// messages (sign-in, refresh, sign-out) is picked up without restarting
    /// the extension process.
    private func makeBridge() -> NativeBridge {
        NativeBridge(
            sender: {
                // The extension process reads the token from the SHARED keychain
                // group, which requires the keychain-access-groups entitlement to
                // be registered in the Apple Developer portal (a one-time GUI
                // step; see the sharing-layer section of the proposal). Until
                // that lands this resolves to nil and the bridge answers
                // needs_sign_in — a truthful "cannot read a token here", never a
                // crash and never a false success.
                let store = KeychainStore(accessGroup: Sharing.keychainAccessGroup)
                let auth = AuthManager(keychain: store)
                guard auth.currentTokens() != nil else { return nil }
                return RelaylessSender(token: AuthManagerTokenProvider(auth))
            },
            context: {
                let store = KeychainStore(accessGroup: Sharing.keychainAccessGroup)
                let auth = AuthManager(keychain: store)
                let resolver = EnsureAttention(
                    token: AuthManagerTokenProvider(auth),
                    cache: UserDefaultsResolvedCache(defaults: Sharing.sharedDefaults())
                )
                let resolved = try await resolver.ensureAttentionDefinitionAndTags()
                // The slug is hash input to source_id. It was the empty string
                // here, which made every Safari install hash identically —
                // an iPhone and a Mac visiting the same URL in the same second
                // produced the same source_id and dedup dropped one. See
                // DeviceIdentity for why this is an automatic per-install id
                // rather than a human label.
                //
                // identityLabel stays nil on purpose: no human has named this
                // device, so claiming a name would be inventing one, and the
                // machine: tag is correctly not minted without it.
                return WireContext(
                    definitionId: resolved.definitionId,
                    tagIds: resolved.tagIds,
                    identitySlug: DeviceIdentity.slug()
                )
            }
        )
    }

    func beginRequest(with context: NSExtensionContext) {
        let request = context.inputItems.first as? NSExtensionItem

        let message: Any?
        if #available(iOS 15.0, macOS 11.0, *) {
            message = request?.userInfo?[SFExtensionMessageKey]
        } else {
            message = request?.userInfo?["message"]
        }

        guard let payload = message as? [String: Any] else {
            os_log(.error, "bridge: message was not a dictionary: %@", String(describing: message))
            complete(context, with: [BridgeKey.ok: false,
                                     BridgeKey.error: "message must be an object"])
            return
        }

        let bridge = makeBridge()
        Task {
            let reply = await bridge.handle(payload)
            complete(context, with: reply)
        }
    }

    private func complete(_ context: NSExtensionContext, with reply: [String: Any]) {
        let response = NSExtensionItem()
        if #available(iOS 15.0, macOS 11.0, *) {
            response.userInfo = [SFExtensionMessageKey: reply]
        } else {
            response.userInfo = ["message": reply]
        }
        context.completeRequest(returningItems: [response], completionHandler: nil)
    }
}
