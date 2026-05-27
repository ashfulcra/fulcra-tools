# Menubar vs Web-UI Drift: Scoping Document

**Date:** 2026-05-27
**Branch HEAD:** `44695f9` (`chore(web-ui): refresh runOnboarding docstring for collect_modes phase`)
**Scope:** `packages/menubar/fulcra_menubar/` — bring it in line with the surfaces the web UI has grown since the menubar was last touched.
**Mode:** Read-only scoping. No implementation. No code.

---

## 1. Current state inventory

The menubar app has **five user-visible surfaces**, all owned by `FulcraMenubarApp` in `app.py`:

### S1 — Status item (menubar icon)
**File:** `packages/menubar/fulcra_menubar/status_item.py`

Renders the 22pt template icon plus four overlays:
- Violet pulse animation while any plugin is `in_flight` (`OverallState.RUNNING`).
- Steady cyan glow while a quick-record Duration timer is ticking.
- Amber dot badge in bottom-right for 1–2 consecutive failures on any enabled plugin (`failing_warning_count > 0`).
- Red dot badge for ≥3 consecutive failures (`failing_critical_count > 0`).
- Whole icon dropped to 40% alpha when the daemon is stopped.

User actions: single left-click opens the popover (wired in `app.py:223` `_install_click_target`). No right-click menu (`setMenu_(None)` at `app.py:94`).

### S2 — Popover (primary view: Quick Record)
**Files:** `popover/root.py`, `popover/quick_record.py`, `popover/header.py`, `popover/daemon_bar.py`

A 360pt-wide NSPopover with four bands stacked top-to-bottom:
- **Header (56pt)** — title "Fulcra Collect", a status pill (Healthy / Running… / Failing / Daemon stopped / Connecting…), a subtitle that summarises the plugin counts ("N plugins · S scheduled · V services · M manual"), and a small gear icon that opens Preferences.
- **Body (variable)** — defaults to the Quick Record view. Renders a scrollable list of annotation definitions sourced from the daemon's `quick_record_list` command, grouped by `annotation_type` ("Moments", "Durations", "Read", "Watched", "Listened"). Each row has:
  - Per-row pin/unpin star (toggles favorites via `set_quick_record_favorites`).
  - Pinned rows get a faint violet background tint.
  - Moment rows: name + comment field + **Record** button.
  - Duration rows: two-line layout — name + comment + inline duration text input (e.g. "90m") + **Record** + **Start/Stop** timer toggle.
  - When the user has any favorites pinned, the body filters to favorites only and a "Show all annotations (N more)" disclosure footer expands the list. The disclosure resets to collapsed on every popover open.
  - A "Recently recorded" footer section appears with up to 5 most-recent entries, each with an **Undo** button that calls `delete_annotation` (soft-delete tombstone — tooltip surfaces this).
- **Daemon-controls bar (64pt)** — "Daemon: Running (PID X)" + Stop + Restart buttons (or Start when stopped, or Install when no plist). Plus an "Open at Login" NSSwitch beneath. State refreshes on every popover open. See `popover/daemon_bar.py`.
- **Footer (36pt)** — single right-aligned **Quit** button. Borderless hairline separator at top.

A "View Status →" button in the Quick Record footer swaps the body to S3.

### S3 — Popover (secondary view: Plugin Status)
**File:** `popover/root.py` (the `_status_container` branch), `popover/plugin_row.py`

Reached from "View Status →" or shown by default when the daemon is stopped (bootstrap card). A "← Quick Record" back-bar at top, then a scrollable list of plugin rows. Each row (`plugin_row.py`, 44pt tall):
- Coloured status dot (gray=disabled, red=any failures, amber=running, mint=healthy — note: this is a coarser mapping than the dashboard's three-tier pill).
- Name + plugin-id.
- Right-hand label: "Running" / "Crashed" for service kind, "Never run" otherwise, or relative time ("5m ago").
- **Run now** button for manual plugins (always shown) and enabled scheduled plugins.

When `daemon_stopped`, the status view shows `bootstrap.py`'s "Fulcra Collect is not running." card with a single CTA to install and start the daemon.

### S4 — Preferences window
**Files:** `preferences/window.py`, `preferences/plugins_tab.py`, `preferences/notifications_tab.py`, `preferences/about_tab.py`

640 × 480 NSWindow with an NSTabView, three tabs:
- **Plugins** — scrollable list with one expanded row per plugin (`_make_plugin_row`, height = `112 + 24 * len(credentials)`). Per row: name + id, description (word-wrapped 32pt block), Enable switch (or Run-now for manual), interval field with "Every N minutes / ≈ 6 hours" caption for scheduled, **Run now** button at the bottom, and a credentials block with Connected/Disconnect or paste-secret/Connect per credential key.
- **Notifications** — two toggles ("Notify me when a plugin fails repeatedly" + "Mute all notifications"). Hard-coded copy says "After 3 consecutive failures. At most one notification per plugin per hour."
- **About** — top action row with **Open Activity Logs** + **Launch at login** switch. Identity block (App version, Daemon version, Config path, State directory). Scrollable plugin-versions list.

### S5 — Native notifications
**File:** `notifications.py` + the UN authorization handshake in `app.py:137`

Posts a macOS notification when any plugin crosses ≥3 consecutive failures. Rate-limited at 1 per plugin per hour. No notification on recovery; no "Run completed" notifications. The user can mute via Preferences → Notifications.

---

## 2. Drift catalogue

The web UI has grown six clusters of new behaviour the menubar hasn't tracked. Each row below is **observed drift**, not aspiration.

### D1 — No soft-delete for annotation tracks
- **Surface:** Preferences (would need a new tab) and/or Quick Record popover (per-row affordance)
- **Drift:** The web UI's `/?route=settings` page (`packages/web-ui/dist/index.html:833+`, `static/settings.js`) lets users soft-delete annotation definitions via `DELETE /api/definitions/{def_id}`. The menubar has no equivalent surface. The daemon's UDS control socket also has no `delete_definition` command — only the HTTP route. So the menubar can't trigger it without a new daemon command or a daemon → HTTP fallback path.
- **Why it matters:** users who create a definition by accident (or via the wizard's "Create new" flow with a bad name) have to context-switch to the web UI to clean up. The pin/unpin star already lives in the popover quick-record rows, so a "Delete this track" affordance has a natural home.
- **Cost-to-fix:** **medium** — needs a daemon UDS command (`delete_definition`), a new `DaemonClient` method, UI affordance(s), and a confirmation flow because soft-delete events are not undoable from the menubar.

### D2 — Quick-record favorites only managed per-row (no bulk view)
- **Surface:** Preferences (would need a new tab)
- **Drift:** The web UI's settings page has a checkbox grid for managing favorites in bulk (`index.html:781-830`). The menubar can only pin/unpin one row at a time from the popover. There's also no Preferences-tab view that surfaces "here are your favorites" without opening the popover.
- **Why it matters:** moderate. The per-row star works fine for incremental edits, but a user who wants to set up favorites for the first time has to scroll the popover and click each one — the web UI is much faster for the bulk case.
- **Cost-to-fix:** **small** — a new "Annotations" or "Quick Record" Preferences tab with a list view. Uses existing daemon endpoints (`get_quick_record_favorites`, `set_quick_record_favorites`). No new daemon work.

### D3 — No in-app docs viewer
- **Surface:** Preferences → About, or popover header, or a new menu item
- **Drift:** The web UI added an in-app docs viewer (`/?route=docs`, `index.html:725-764`) that fetches markdown via `GET /api/docs/{name}` and renders it inline. The dashboard's "Data sources" link in the header navigates to it. The menubar has no equivalent — the closest thing is the **Open Activity Logs** button on the About tab.
- **Why it matters:** the same need that drove the web UI's in-app docs exists in the menubar (the github blob URL 404s while the repo is private). A user reading "Why is this Apple Podcasts plugin failing?" has no in-menubar path to the docs.
- **Cost-to-fix:** **small** — the daemon already exposes `GET /api/docs/{name}`. The menubar needs either a new "Help" affordance that opens the daemon's web UI to the docs route (cheap), or a native NSScrollView markdown renderer (more work, more value).

### D4 — Plugin-status row action set is narrower than the dashboard
- **Surface:** Popover → Plugin Status view (S3), Preferences → Plugins tab
- **Drift:** The web UI dashboard's per-plugin action set is `{Run now, Configure, Disable}` plus a richer three-tier pill (`Disabled` / `Failing` red / `Failed — run again` amber / `Running` violet / `Healthy` mint / `Not run yet` slate — see `static/dashboard.js:70-104`). The popover's plugin rows only have **Run now** (no Configure, no Disable). The popover's coloured dot is a coarser two-tier (red on any-failure, amber on running, mint else, gray on disabled) — it does NOT distinguish 1–2 failures from ≥3 like the dashboard pill (and like the menubar's own icon-badge code in `status_item.py:130-137`).
- **Why it matters:** the popover and the icon-badge layer use **different** failure-count mappings — internally inconsistent. The dashboard's amber "Failed — run again" pill exists precisely to surface the common case (paired Attention, but the diagnostic run before pairing left `last_outcome="error"`). The popover hides this distinction.
- **Cost-to-fix:** **small** (pill mapping unification: just update `plugin_row.py:_status_dot`) plus **medium** if adding Configure + Disable actions to the popover (Disable already exists in Preferences via the Enable switch, but Configure has no menubar equivalent — the dashboard's Configure routes to a per-plugin wizard which is the entire web UI's onboarding flow).

### D5 — No surfacing of historical vs live framing
- **Surface:** Plugin Status view (S3), Preferences → Plugins tab, Quick Record (less so)
- **Drift:** The web UI introduced a `collect_modes` onboarding screen (`index.html:212-360`) that frames each plugin as **historical** (one-time export — Apple Music takeout, Netflix takeout) or **live** (continuous capture — Last.fm, Trakt, Attention). It calls out that they're safe to mix because Fulcra deduplicates. This framing is **absent from the menubar**. The popover and Preferences show only "scheduled / service / manual" kinds, which is a technical taxonomy, not the user-facing historical/live one.
- **Why it matters:** the user just walked through `collect_modes` during onboarding and saw "Last.fm — live" / "Apple Music takeout — historical" framed prominently. Opening the menubar and seeing only "scheduled / service / manual" is a hard mental gear shift. They lose the connection between what they configured and what's running.
- **Cost-to-fix:** **medium** — needs a per-plugin `collect_mode` attribute on the plugin metadata (probably in `fulcra-collect/registry.py`) propagated through the daemon's `status` reply, then a label or icon in the popover row and Preferences row.

### D6 — Cross-source unification framing not surfaced anywhere
- **Surface:** none — would be net-new in the menubar
- **Drift:** This session landed `cross_source_fingerprint.py` so a Last.fm play and an Apple Music takeout play of the same track collapse into one unified `Listened` track. The menubar doesn't surface this anywhere — neither in the popover row tooltip, nor in the Preferences plugin description, nor in any "what's actually in your Fulcra timeline" view.
- **Why it matters:** low-to-moderate. Users who set up overlapping historical + live sources see two plugin rows running and have no signal that the data is being deduped. The web UI's `collect_modes` screen carries this messaging at onboarding time but it isn't surfaced ongoing.
- **Cost-to-fix:** **small** if it's just a tooltip / description-line addition; **medium** if it's a new view.

### D7 — Plugin description text exists but spacing makes it cramped
- **Surface:** Preferences → Plugins tab, popover → Plugin Status (S3)
- **Drift:** The Preferences plugin row uses `_make_plugin_row` with `row_height = 112 + 24 * len(credentials)` (`plugins_tab.py:117`). The description is allotted a fixed 32pt block (`plugins_tab.py:148`) which truncates anything past two lines and overlaps the interval block at `height - 88` (line 192). The popover plugin row (`plugin_row.py`) skips the description entirely. Meanwhile the web UI shows the description prominently in both the dashboard row and the wizard.
- **Cost-to-fix:** **small** — recompute description height dynamically and shift the interval/credentials blocks down accordingly.

### Drift summary by cost-to-fix

- **Small (5):** D2 (favorites bulk view), D3 (in-app docs link), D4 pill mapping only, D6 unification framing, D7 description spacing.
- **Medium (3):** D1 (soft-delete), D4 with Configure+Disable, D5 (historical/live label).
- **Large (0):** none of the drift items are large by themselves. The aggregate is medium-sized.

---

## 3. Spacing/layout findings

Walking the AppKit frames hand by hand against the actual rendered widths, the worst offenders are:

### L1 — Quick Record Duration row crams 4 controls into 360pt with no margin (popover/quick_record.py:687-727)
- The Record button at `x = 16 + 140 + 6 + 64 + 6` = **228**, width 56 → ends at **284**.
- The Timer button at `width - 56 - 12` = **292**, width 56 → ends at **348**.
- **The Record button ends at 284 and the Timer button starts at 292 — only 8pt clearance** between two buttons that do different things. The user is one mis-click away from starting a timer when they meant to record a one-shot duration.
- The comment field at width 140 is too narrow for typical use.
- The two-line layout stacks name+star+timer-hint **above** the input row, making the Duration row 84pt tall — taller than the moment row (44pt), so a Duration is twice the visual weight of a Moment in the same list. Inconsistent rhythm.

### L2 — Preferences plugin row description overlaps interval block (preferences/plugins_tab.py:148, 192)
- The description label is positioned at `height - 60` with height 32 — fixed 32pt block clips at 2 lines worth. There's no min-height adjustment for descriptions longer than what 32pt fits. The user reading e.g. "Plex/Jellyfin webhook receiver — listens on :8765 for scrobble webhooks from your media server. See docs for setup" gets a clipped description.

### L3 — Preferences About tab launch-at-login row is misaligned (preferences/about_tab.py:82-105)
- `ACTION_Y = 396`. Open Activity Logs button at `y=ACTION_Y` height 28 (vertical center ≈ 410). Launch-at-login label at `y=401` height 18 (center ≈ 410). NSSwitch at `y=398` height 22 (center 409) — off by 1pt, invisible.
- BUT: the caption "Open Fulcra Collect automatically..." is at `y=380`, the separator is at `y=368`, and the identity block starts at `y=340` with the first row of labels at y=340 height 16 — top at 356, way too close to the separator at y=368.
- **Net effect: three rows of text (label / switch / caption / separator / first identity row) crammed into a 60pt vertical span.** This is the most obvious "spacing is really bad" — the user almost certainly meant the About tab.

### Top 3 layout findings for the user

1. **Preferences → About tab top action row (`about_tab.py:67-114`)** — Open Activity Logs button + Launch-at-login label/switch/caption + separator + identity rows stuffed into a 60pt vertical span starting at y=340–400. The separator at y=368 is only 12pt below the caption and only 12pt above the first identity row. Very dense, looks broken.
2. **Quick Record Duration row Record/Timer button spacing (`quick_record.py:707-727`)** — Record button ends at x=284, Timer starts at x=292. Only 8pt clearance between two buttons that do very different things. Easy to mis-click.
3. **Preferences → Plugins description label clips 3+ line descriptions (`plugins_tab.py:148`)** — fixed 32pt height block doesn't grow for long descriptions, so descriptions get clipped without the user knowing there's more text.

---

## 4. Historical-vs-live framing applied to the menubar

The web UI's `collect_modes` screen frames plugins as **historical** (one-shot takeout/exports) vs **live** (continuous capture). Three ways this could surface in the menubar:

### Option A — per-row inline label
Each popover plugin row and each Preferences plugin row gets a small chip after the plugin name: `📅 Historical` or `🌊 Live`. Cheap, visible. Forces a new per-plugin `collect_mode` field in the plugin metadata.

### Option B — group the Plugin Status view by collect_mode instead of by kind
Today the popover groups plugins as `service` / `scheduled` / `manual`. Replace this with `Live (continuous)` / `Live (polled)` / `Historical (one-shot)`. Closer to the web UI's mental model. Also a relatively cheap rendering change once the metadata field exists.

### Option C — skip; menubar isn't the right surface
The argument: `collect_modes` is an onboarding screen, designed to set the user's mental model once. Repeating it in the menubar may add noise without value.

### Recommendation: **Option B**, with a fallback to Option A for items in Preferences.

Reasoning: the popover's existing grouping by `kind` is **already** a taxonomy choice — not free real estate. Swapping it for the user-facing historical/live taxonomy aligns the menubar with the language the user just learned during onboarding. The `manual` kind maps to "Historical (one-shot)"; `scheduled` to "Live (polled)"; `service` to "Live (continuous)". The mapping is already 1:1 with kind in practice, so the implementation cost is mostly renaming labels.

---

## 5. Implementation strategy proposal

Four sub-projects, ordered by dependency:

### SP1 — Spacing/layout fixes (no new functionality)
**Scope:** L1, L2, L3 from §3. Strict UI polish.
**Prereqs:** none.
**Sizing:** small (~½ day).
**Why first:** the user explicitly flagged this. It unblocks visual confidence in everything else and doesn't introduce any new daemon contract.

### SP2 — Annotation-management Preferences tab (D1 + D2)
**Scope:** New "Annotations" tab in Preferences with bulk favorites checkbox list + soft-delete with confirmation dialog.
**Prereqs:**
- New daemon UDS command `delete_definition` wrapping `_delete_definition` from `routes/definitions.py:225`.
- New `DaemonClient.delete_definition()` method.
**Sizing:** medium (~1–2 days).
**Why second:** closes the most concrete drift gap the user named.

### SP3 — Historical/live framing (D5) and pill-mapping unification (D4 partial)
**Scope:**
- Add `collect_mode: Literal["historical", "live_polled", "live_continuous"]` to plugin metadata; propagate through daemon `status` reply.
- Re-group popover Plugin Status view by `collect_mode` (Option B).
- Add per-row collect_mode chip in Preferences → Plugins.
- Unify the popover's status-dot mapping with the dashboard's three-tier pill so popover dot, icon badge, and web dashboard all agree.
**Prereqs:** none in the menubar; requires `fulcra-collect` registry changes that touch every plugin module's metadata.
**Sizing:** medium (~1–2 days), mostly because every plugin needs a `collect_mode` value reviewed.

### SP4 — In-app docs + Configure/Disable actions (D3 + D4 remainder)
**Scope:**
- Add a "Help / Docs" affordance — either menu item that opens the web UI docs route, or native NSScrollView markdown renderer.
- Add **Configure** + **Disable** buttons to the popover plugin rows.
**Prereqs:** none beyond SP3's metadata.
**Sizing:** small (~1 day) for the cheap "open in browser" path; medium for native markdown rendering.

---

## 6. Open questions for the user

**Q1 — Where should the new annotation-management UI live?**
- (a) A new Preferences tab called "Annotations" — discoverable, parallel to Plugins/Notifications/About.
- (b) A "Manage tracks" button in the popover footer that opens a dedicated NSWindow.
- (c) Per-row in the popover (each quick-record row gets a "..." menu with "Soft-delete this track").
**Recommendation:** (a) for soft-delete + bulk favorites, plus a small "..." per-row in the popover (c) for the one-off case.

**Q2 — Should the historical/live framing replace the kind taxonomy or augment it?**
- (a) Replace: drop "scheduled / service / manual" from user-facing UI entirely.
- (b) Augment: keep kind in Preferences (where the technical truth matters), use historical/live in the popover.
- (c) Skip the framing; keep kind.
**Recommendation:** (b).

**Q3 — In-app docs viewer: open in browser, or native markdown render?**
- (a) Open in system browser at `http://127.0.0.1:9292/?route=docs` (cheap; full-fidelity).
- (b) Native NSAttributedString markdown render inside popover/window.
- (c) "Docs" button that opens just the system browser to the doc; no in-app view.
**Recommendation:** (a).

**Q4 — Soft-delete confirmation: how aggressive?**
- (a) Simple NSAlert "Delete X? This removes it from pickers. Events already written stay in your timeline."
- (b) Two-step: NSAlert + a 5-second "Are you really sure?" delay.
- (c) Just delete; rely on undo (which doesn't exist yet).
**Recommendation:** (a).

**Q5 — Where should the per-plugin Configure action route to?**
- (a) Always open the web UI wizard at `/?route=configure&plugin=<id>`.
- (b) For credentials-only plugins, open a native modal; for wizards, fall through to web UI.
- (c) Hide Configure on popover entirely; users edit via Preferences → Plugins.
**Recommendation:** (a). Wizards are complex and routinely change; we should not re-implement them.

**Q6 — Should the quick-record Duration row layout change at all in SP1?**
- (a) Tighten by removing the Record button entirely — timer is the primary; inline "90m + Record" moves into a disclosed second line.
- (b) Keep both controls but widen the gap and shrink the comment field.
- (c) Keep both, widen the popover from 360 → 400pt.
**Recommendation:** (b) — (a) loses the "log retroactive duration" workflow, (c) breaks the popover's width contract.

**Q7 — Should we open a docs link from the popover header (next to the gear), or only from Preferences?**
- (a) Header — a "?" icon next to the gear. Always one click away.
- (b) Preferences only — keeps the header clean.
- (c) Both.
**Recommendation:** (a). The header has room.

---

## 7. Recommended sequencing

**Start with SP1 (spacing/layout fixes).**

Reasoning:
- The user explicitly flagged it. Doing it first signals we heard them.
- It's the smallest and lowest-risk: no daemon contract changes, no new state, no new plugin metadata. Three localised edits to existing files.
- It unlocks SP2's "new tab in Preferences" with confidence that the tab inherits a polished spacing language.
- It produces visible user-facing wins quickly, which builds momentum for the larger sub-projects.

Suggested order: **SP1 → SP2 (in parallel with SP3 in two agents) → SP4**.

---

## Critical files for implementation
- `packages/menubar/fulcra_menubar/preferences/about_tab.py`
- `packages/menubar/fulcra_menubar/preferences/plugins_tab.py`
- `packages/menubar/fulcra_menubar/popover/quick_record.py`
- `packages/menubar/fulcra_menubar/popover/plugin_row.py`
- `packages/menubar/fulcra_menubar/daemon_client.py`
- `packages/collect/fulcra_collect/` (for SP3's metadata propagation through `status`)
