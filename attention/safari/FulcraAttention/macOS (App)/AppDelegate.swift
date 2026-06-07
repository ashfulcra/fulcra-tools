//
//  AppDelegate.swift
//  macOS (App)
//
//  Created by Ash Kalb on 6/7/26.
//

import Cocoa
import SwiftUI
import os

private let appLog = Logger(subsystem: "com.fulcra.attention", category: "AppDelegate")

@main
class AppDelegate: NSObject, NSApplicationDelegate {

    func applicationDidFinishLaunching(_ notification: Notification) {
        appLog.info("application did finish launching")
        // Inject the SwiftUI "Sign in to Fulcra" panel above the converter's
        // web view (which carries the "enable the extension in Safari" content).
        installSignInPanel()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    /// Add the SwiftUI sign-in panel to the top of the main window's content
    /// view, leaving the storyboard-loaded web view in place below it.
    private func installSignInPanel() {
        guard let window = NSApplication.shared.windows.first,
              let contentView = window.contentView else {
            appLog.error("no main window content view; sign-in panel not installed")
            return
        }

        let hosting = NSHostingView(rootView: SignInView())
        hosting.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(hosting)

        NSLayoutConstraint.activate([
            hosting.topAnchor.constraint(equalTo: contentView.topAnchor),
            hosting.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            hosting.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
        ])
        appLog.info("sign-in panel installed")
    }

}
