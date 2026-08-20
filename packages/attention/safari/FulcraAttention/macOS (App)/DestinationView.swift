//
//  DestinationView.swift
//  FulcraAttention
//
//  Shows — and lets the user change — WHICH Fulcra "Attention" annotation this
//  device writes into.
//
//  WHY THIS EXISTS. EnsureAttention resolves the destination automatically:
//  it lists the account's "Attention" duration definitions, sorts them
//  oldest-first, and adopts index 0. That is a sensible DEFAULT and it is not a
//  substitute for telling the user where their data is going.
//
//  Chrome has always had a destination step in its onboarding wizard. Safari
//  shipped without one, so a user installed the app, browsed, and had no way to
//  learn which definition received it — or to send it somewhere else. That gap
//  is what this view closes.
//
//  It is worth being precise about the failure it prevents, because "it worked
//  for me" hides it. Oldest-first is an ARBITRARY TIEBREAK, not intent. With a
//  single "Attention" definition it is always right. With two — and duplicate
//  definitions demonstrably occur in real accounts — it silently picks the
//  older one, which may be the empty one someone created by accident. The data
//  still lands, still validates, and is simply in the wrong place, discoverable
//  only by noticing an absence.
//
//  The whole destination API (list / choose / create) already existed in
//  EnsureDefinition.swift, fully unit-tested and parity-checked against the
//  TypeScript. It had NO production callers. Passing tests made it look
//  finished; nothing on screen could reach it. This file is the wiring.
//

import SwiftUI
import Combine
import os

private let destLog = Logger(subsystem: "com.fulcra.attention", category: "DestinationView")


/// The three operations the destination UI needs, as a seam.
///
/// This exists because the first version of these tests could not fail: they
/// wrote the cache double directly and never called the view model, so they
/// stayed green with the production wiring deleted — the precise defect this
/// whole file was written to fix, reproduced inside its own tests. A protocol
/// makes "did the view model actually call the service" an assertable fact
/// rather than an assumption.
public protocol DestinationService: Sendable {
    func listDestinations() async throws -> [AttentionDestination]
    func choose(definitionId: String) async throws -> ResolvedAttention
    func create(name: String) async throws -> ResolvedAttention
}

extension EnsureAttention: DestinationService {
    public func listDestinations() async throws -> [AttentionDestination] {
        try await listAttentionDestinations()
    }
    public func choose(definitionId: String) async throws -> ResolvedAttention {
        try await chooseAttentionDestination(definitionId: definitionId)
    }
    public func create(name: String) async throws -> ResolvedAttention {
        try await createAttentionDestination(name: name)
    }
}

@MainActor
public final class DestinationViewModel: ObservableObject {

    public enum Status: Equatable {
        case idle
        case loading
        /// `current` is nil when nothing is cached yet (nothing has been resolved).
        case loaded(destinations: [AttentionDestination], current: String?)
        case error(String)
    }

    @Published public private(set) var status: Status = .idle
    @Published public private(set) var busy = false

    private let service: DestinationService
    private let cache: ResolvedAttentionCache

    public init(
        service: DestinationService? = nil,
        cache: ResolvedAttentionCache = UserDefaultsResolvedCache(defaults: Sharing.sharedDefaults())
    ) {
        self.cache = cache
        self.service = service ?? EnsureAttention(
            token: AuthManagerTokenProvider(AuthManager(keychain: KeychainStore(accessGroup: Sharing.keychainAccessGroup))),
            cache: cache
        )
    }

    /// The definition id currently cached, i.e. the one the EXTENSION will use
    /// on its next flush. Read straight from the shared cache rather than from
    /// this view's state so what is displayed is what actually ships.
    public var cachedDefinitionId: String? { cache.read()?.definitionId }

    public func load() async {
        status = .loading
        do {
            let list = try await service.listDestinations()
            status = .loaded(destinations: list, current: cachedDefinitionId)
        } catch {
            destLog.error("load failed: \(error.localizedDescription, privacy: .public)")
            status = .error(error.localizedDescription)
        }
    }

    /// Adopt an existing definition. Writes the shared cache, so the extension
    /// picks it up on its next resolve without an app restart.
    public func choose(_ id: String) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await service.choose(definitionId: id)
            destLog.info("destination set to \(id, privacy: .public)")
            await load()
        } catch {
            status = .error(error.localizedDescription)
        }
    }

    /// Create a brand-new definition and adopt it. Separate from `choose` on
    /// purpose: creating is how duplicates get made, so it must be an explicit
    /// act, never a fallback.
    public func createNew(name: String) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await service.create(name: name)
            await load()
        } catch {
            status = .error(error.localizedDescription)
        }
    }
}

public struct DestinationView: View {
    @StateObject private var model: DestinationViewModel
    @State private var newName: String = ATTENTION_DEFINITION_NAME
    @State private var showCreate = false

    @MainActor public init() { _model = StateObject(wrappedValue: DestinationViewModel()) }
    public init(model: DestinationViewModel) { _model = StateObject(wrappedValue: model) }

    public var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Where your attention is saved")
                .font(.headline)

            switch model.status {
            case .idle, .loading:
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Checking…").foregroundStyle(.secondary)
                }

            case let .error(message):
                Label(message, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.red)
                Button("Try again") { Task { await model.load() } }

            case let .loaded(destinations, current):
                if destinations.isEmpty {
                    Text("No Attention annotation exists yet. One will be created when capture first runs.")
                        .foregroundStyle(.secondary)
                } else {
                    // Never silently show a resolved-looking row when nothing is
                    // cached: "not chosen yet" is a different state from "chosen",
                    // and conflating them is how the original gap read as fine.
                    if current == nil {
                        Text("Not chosen yet — capture will use the default below.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    ForEach(destinations, id: \.id) { d in
                        row(d, isCurrent: d.id == current)
                    }
                }

                if showCreate {
                    HStack {
                        TextField("Name", text: $newName)
                        Button("Create") { Task { await model.createNew(name: newName); showCreate = false } }
                            .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty || model.busy)
                        Button("Cancel") { showCreate = false }
                    }
                } else {
                    Button("Create a new one…") { showCreate = true }
                        .disabled(model.busy)
                }
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .task { await model.load() }
    }

    @ViewBuilder
    private func row(_ d: AttentionDestination, isCurrent: Bool) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: isCurrent ? "largecircle.fill.circle" : "circle")
                .foregroundStyle(isCurrent ? Color.accentColor : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(d.name)
                    // The id is shown because two definitions share the name
                    // "Attention" — the name alone cannot tell them apart, which
                    // is the entire problem this view exists to make visible.
                    Text(String(d.id.prefix(8)))
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if d.isAutoPick {
                        Text("default").font(.caption2)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(Color.secondary.opacity(0.15)))
                    }
                }
                if let created = d.createdAt {
                    Text("created \(created)").font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if !isCurrent {
                Button("Use this") { Task { await model.choose(d.id) } }
                    .disabled(model.busy)
            }
        }
    }
}
