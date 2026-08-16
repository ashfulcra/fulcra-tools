//
//  SceneDelegate.swift
//  iOS (App)
//
//  Created by Ash Kalb on 6/7/26.
//

import UIKit
import SwiftUI
import os

private let appLog = Logger(subsystem: "com.fulcra.attention", category: "iOSApp")

class SceneDelegate: UIResponder, UIWindowSceneDelegate {

    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
        guard let _ = (scene as? UIWindowScene) else { return }
        installSignInPanel()
    }

    /// Put the SwiftUI sign-in panel above the storyboard's web view, mirroring
    /// what AppDelegate does on macOS with NSHostingView.
    ///
    /// This exists because the iOS app is where a user SIGNS IN. The extension
    /// deliberately holds no credentials — it reads the token the app stored —
    /// so without this panel a TestFlight build installs, enables the extension,
    /// and then has no way to authenticate at all.
    ///
    /// Installed as a CHILD view controller rather than a bare view: a
    /// UIHostingController that is not in the responder chain loses SwiftUI's
    /// lifecycle and environment, which is the sort of thing that looks fine on
    /// screen and then misbehaves on rotation or backgrounding.
    private func installSignInPanel() {
        guard let root = window?.rootViewController else {
            appLog.error("no root view controller; sign-in panel NOT installed")
            return
        }

        let hosting = UIHostingController(rootView: SignInView())
        hosting.view.translatesAutoresizingMaskIntoConstraints = false
        root.addChild(hosting)
        root.view.addSubview(hosting.view)
        hosting.didMove(toParent: root)

        NSLayoutConstraint.activate([
            hosting.view.topAnchor.constraint(equalTo: root.view.safeAreaLayoutGuide.topAnchor),
            hosting.view.leadingAnchor.constraint(equalTo: root.view.leadingAnchor),
            hosting.view.trailingAnchor.constraint(equalTo: root.view.trailingAnchor),
        ])
        appLog.info("sign-in panel installed")
    }

}
