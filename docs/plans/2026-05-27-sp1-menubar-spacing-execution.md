# SP1: Menubar Spacing Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three "spacing is really bad" issues the user flagged in the menubar app: the About-tab top action row that crams label + switch + caption + separator + identity rows into 60pt; the Quick-record Duration row where Record and Timer buttons sit 8pt apart; the Plugins-tab description label that clips at 2 lines.

**Architecture:** Three localised AppKit y-coord and width adjustments. No new files. No daemon changes. No new state. No new dependencies. The menubar app is pure Python + PyObjC.

**Tech Stack:** Python 3.12+, PyObjC (AppKit), rumps. The menubar's source lives at `/Users/Scanning/Developer/fulcra-tools/packages/menubar/fulcra_menubar/`.

**Source spec:** `docs/plans/2026-05-27-menubar-vs-webui-drift-scope.md` (committed at `41b29ee`), §3 "Spacing/layout findings" — the three top findings (L1, L2, L3).

**User-confirmed direction:** scoping-doc Q6 was answered "Keep both controls, widen gap" for the Duration row. The other two fixes have only one sensible shape (more vertical breathing room / dynamic description height).

**Reading list before starting:**
- `packages/menubar/fulcra_menubar/preferences/about_tab.py` — the About tab layout (Task 1 surface).
- `packages/menubar/fulcra_menubar/popover/quick_record.py:680-735` — Duration row layout (Task 2 surface).
- `packages/menubar/fulcra_menubar/preferences/plugins_tab.py:110-205` — Plugins-tab row layout (Task 3 surface).

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `packages/menubar/fulcra_menubar/preferences/about_tab.py` | Modify | About tab y-coords expanded for 24pt breathing room between caption / separator / identity rows. |
| `packages/menubar/fulcra_menubar/popover/quick_record.py` | Modify | Duration row's comment field shrunk from 140 → 120pt to create 24pt clearance between Record and Timer buttons. |
| `packages/menubar/fulcra_menubar/preferences/plugins_tab.py` | Modify | Description label uses its actual word-wrapped height (capped at 80pt), and the row's overall height grows to fit. |

Total surface: ~30 lines modified across 3 files.

---

## Task 1: About tab — vertical breathing for the top action row (L3)

**File:** `packages/menubar/fulcra_menubar/preferences/about_tab.py`

**Why:** Currently the caption→separator gap is 12pt and the separator→first identity row gap is also 12pt. Combined with the 1pt separator itself and 16pt label heights, the user sees a wall of text crammed together at the top. The fix doubles each gap to 24pt, pushing the identity block + scrollable plugin-versions list down by 24pt total. The scroll area shrinks by 24pt; the plugin-versions list already scrolls, so this has no functional impact.

- [ ] **Step 1: Update the separator y-coord.**

Find this block (around line 110-114):

```python
    # ------------------------------------------------------------------
    # Thin separator line (via a plain view) between action row and identity.
    # ------------------------------------------------------------------
    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, ACTION_Y - 28, _TAB_W - 32, 1))
```

Replace the separator frame `NSMakeRect(16, ACTION_Y - 28, ...)` with `NSMakeRect(16, ACTION_Y - 40, ...)`:

```python
    # ------------------------------------------------------------------
    # Thin separator line (via a plain view) between action row and identity.
    # Sits 24pt below the caption (which is at ACTION_Y - 16). The doubled
    # gap (was 12pt) closes the "About tab is crammed" finding from the
    # 2026-05-27 menubar drift audit.
    # ------------------------------------------------------------------
    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, ACTION_Y - 40, _TAB_W - 32, 1))
```

- [ ] **Step 2: Shift the identity block down by 24pt total.**

Find the four `_info_row` calls around line 132-135:

```python
    _info_row("App version", app_version,     ACTION_Y - 56)
    _info_row("Daemon version", daemon_version, ACTION_Y - 76)
    _info_row("Config",        str(_config.config_dir() / "config.toml"), ACTION_Y - 96)
    _info_row("State directory", str(_config.config_dir() / "state"),    ACTION_Y - 116)
```

Replace each y-offset, shifting everything down 24pt (so the gap between the separator at y=ACTION_Y-40 and the first identity row at y=ACTION_Y-80 becomes ~24pt; 16pt row height + 24pt gap = 40pt vertical span between separator and first row's baseline, which leaves the user with comfortable breathing room):

```python
    _info_row("App version", app_version,     ACTION_Y - 80)
    _info_row("Daemon version", daemon_version, ACTION_Y - 100)
    _info_row("Config",        str(_config.config_dir() / "config.toml"), ACTION_Y - 120)
    _info_row("State directory", str(_config.config_dir() / "state"),    ACTION_Y - 140)
```

- [ ] **Step 3: Shift the scroll-area top down by 24pt and shrink its height accordingly.**

Find the SCROLL_TOP block (around line 141-143):

```python
    SCROLL_TOP = ACTION_Y - 140   # top of the scroll area (y of its frame)
    SCROLL_H   = SCROLL_TOP       # fill remaining space down to y=0
    SCROLL_Y   = 0                # bottom of the scroll area
```

Replace with:

```python
    SCROLL_TOP = ACTION_Y - 164   # 24pt below the last identity row (was -140 pre-spacing-fix).
    SCROLL_H   = SCROLL_TOP       # fill remaining space down to y=0.
    SCROLL_Y   = 0                # bottom of the scroll area.
```

- [ ] **Step 4: Verify Python syntax.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/preferences/about_tab.py').read())"
```

Expected: no output (success). Or alternatively, run the menubar's package-level syntax check if one exists.

- [ ] **Step 5: Commit.**

```bash
git add packages/menubar/fulcra_menubar/preferences/about_tab.py
git commit -m "fix(menubar): widen breathing room in About-tab top action row (SP1 L3)

The About tab's top action row crammed five visual elements into a
60pt vertical span: Open-Activity-Logs button, Launch-at-login label,
NSSwitch, caption, separator, and the first identity row. The
separator was only 12pt below the caption and the first identity row
was only 12pt below the separator — \"spacing is really bad\" per the
2026-05-27 menubar drift audit.

Double each gap to 24pt:
  - Caption (y=ACTION_Y-16=380) → separator: 12pt → 24pt.
  - Separator → first identity row: 12pt → 24pt (plus the row's
    16pt height, so the visual gap reads as 40pt).

Net effect: identity block shifts down 24pt; the scrollable
plugin-versions list at the bottom shrinks by 24pt. The list
scrolls anyway, so there's no functional impact.

Pure y-coord adjustment. No daemon changes."
```

---

## Task 2: Quick-record Duration row — widen Record↔Timer gap (L1)

**File:** `packages/menubar/fulcra_menubar/popover/quick_record.py`

**Why:** The Record button currently ends at x=284 and the Timer button starts at x=292. 8pt clearance between two buttons that do very different things (Record = log a one-shot duration; Timer = start/stop a running timer). One mis-click and the user starts a timer when they meant to log retroactively. Shrinking the comment field from 140 → 120pt shifts the Record button left, opening a 24pt gap.

- [ ] **Step 1: Shrink COMMENT_W from 140 to 120.**

Find the constants block around line 687-690:

```python
    # Line 2: comment field, inline-duration field, Record-inline button,
    # Start/Stop timer button.
    COMMENT_W = 140.0
    DURATION_W = 64.0
    RECORD_W = 56.0
    TIMER_W = 56.0
```

Replace `COMMENT_W = 140.0` with `COMMENT_W = 120.0` and update the surrounding comment to explain why:

```python
    # Line 2: comment field, inline-duration field, Record-inline button,
    # Start/Stop timer button.
    #
    # Widths are tuned so the Record and Timer buttons sit ~24pt apart —
    # they fire very different actions (Record = log a one-shot duration;
    # Timer = start/stop a running timer) and used to be 8pt apart, easy
    # to mis-click. If you widen COMMENT_W back toward 140, the gap
    # collapses again. See SP1 L1 in the 2026-05-27 menubar drift audit.
    COMMENT_W = 120.0
    DURATION_W = 64.0
    RECORD_W = 56.0
    TIMER_W = 56.0
```

The downstream layout math (`16 + COMMENT_W + 6 + DURATION_W + 6` for the Record button's x; `width - TIMER_W - 12` for the Timer button's x) automatically picks up the new value. Record now starts at `16+120+6+64+6 = 212`, ends at `268`. Timer is at `360-56-12 = 292`. Gap = `292-268 = 24pt` ✓.

- [ ] **Step 2: Verify Python syntax.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/popover/quick_record.py').read())"
```

Expected: no output.

- [ ] **Step 3: Commit.**

```bash
git add packages/menubar/fulcra_menubar/popover/quick_record.py
git commit -m "fix(menubar): widen Duration-row Record↔Timer button gap (SP1 L1)

Two buttons that fire wildly different actions sat 8pt apart in the
Quick-record Duration row — Record logs a one-shot duration, Timer
starts/stops a running timer. One mis-click and the user is in the
wrong state.

Shrink COMMENT_W from 140 to 120 so the Record button slides left
to end at x=268; Timer stays at x=292. Gap is now 24pt.

The comment field is 20pt narrower as a result. Acceptable
tradeoff: most quick-record comments are short (\"reading\",
\"walked the dog\"); the alternative was widening the popover to
400pt and breaking its longstanding width contract.

Trade-off captured in the constants-block comment for the next
person who tries to widen the comment field. See SP1 L1 in the
2026-05-27 menubar drift audit."
```

---

## Task 3: Plugins-tab description — dynamic height (L2)

**File:** `packages/menubar/fulcra_menubar/preferences/plugins_tab.py`

**Why:** The description label is currently allotted a fixed 32pt height block (2 lines worth). Anything longer is silently clipped — the user reading e.g. "Plex/Jellyfin webhook receiver — listens on :8765 for scrobble webhooks from your media server. See docs for setup" never sees "See docs for setup". The fix: compute the description's actual needed height (capped at 80pt, ~5 lines) and grow the row to fit.

The current row_height formula is `112 + 24 * len(credentials)` where the 112 breaks down as: 28 name + 32 description + 28 interval + 24 run button. We'll change it to: `28 name + max(32, actual_desc_h) + 28 interval + 24 run button`, plus the credentials. The label below the description (interval label or first credential row) stays at a fixed offset from the description's bottom edge.

The cleanest shape: compute the description height OUTSIDE `_make_plugin_row` (in the `rebuild()` closure where row_height is calculated) and pass it in.

- [ ] **Step 1: Add a helper that computes a description's word-wrapped height.**

Near the top of `plugins_tab.py` (after imports, before `build_plugins_tab`), add:

```python
def _compute_desc_height(text: str, width: float, font, cap: float = 80.0) -> float:
    """Measure the rendered height of a word-wrapped description label.

    Why: the description block used to be a hardcoded 32pt — anything
    past 2 lines was silently clipped. Computing the real height lets
    the row grow to fit (capped so a runaway description doesn't
    swallow the whole tab). See SP1 L2 in the 2026-05-27 menubar
    drift audit.

    Args:
        text: the description string.
        width: pixel width the label will be laid out into.
        font: NSFont to render with.
        cap: maximum height we'll allow; anything taller will scroll
             behind clipping (acceptable since the truncation is now
             very rare — ~5 lines of small text is plenty for our
             actual plugin descriptions).
    """
    if not text:
        return 32.0
    attrs = {NSFontAttributeName: font}
    ns_text = NSString.stringWithString_(text)
    bound = ns_text.boundingRectWithSize_options_attributes_(
        NSMakeSize(width, 1000.0),
        NSStringDrawingUsesLineFragmentOrigin,
        attrs,
    )
    needed = float(bound.size.height) + 4.0  # 4pt visual padding
    return min(max(needed, 32.0), cap)
```

You'll need these imports at the top of the file if they aren't already present (check the existing imports first; many of these are likely already imported via the file's blanket `from AppKit import *` or similar — if so, skip them):

```python
from AppKit import NSFontAttributeName, NSStringDrawingUsesLineFragmentOrigin
from Foundation import NSString, NSMakeSize
```

- [ ] **Step 2: Update the row-height calculation in `rebuild()`.**

Find the row-construction loop around line 113-122:

```python
        ordered = sorted(model.plugins, key=lambda p: (p.kind, p.name))
        for snap in ordered:
            credentials = cred_map.get(snap.id, {})
            # Base 112 pt: 28 name + 32 description + 28 interval-or-pad + 24 run btn
            row_height = 112 + 24 * len(credentials)
            row = _make_plugin_row(snap, width, row_height, credentials=credentials,
                                   client=client, model=model)
            row.setFrame_(NSMakeRect(0, y, width, row_height))
            content.addSubview_(row)
            y += row_height
```

Replace with:

```python
        ordered = sorted(model.plugins, key=lambda p: (p.kind, p.name))
        for snap in ordered:
            credentials = cred_map.get(snap.id, {})
            # Description block grows to fit — capped at 80pt so a runaway
            # description can't swallow the whole tab. The 4 fixed regions
            # of the row are: 28 name + desc_h + 28 interval-or-pad + 24
            # run btn, plus 24pt per credential. See SP1 L2 in the
            # 2026-05-27 menubar drift audit.
            desc_h = _compute_desc_height(
                snap.description or "",
                width - 120,
                typography.small(),
            )
            row_height = 28 + desc_h + 28 + 24 + 24 * len(credentials)
            row = _make_plugin_row(snap, width, row_height, desc_h=desc_h,
                                   credentials=credentials,
                                   client=client, model=model)
            row.setFrame_(NSMakeRect(0, y, width, row_height))
            content.addSubview_(row)
            y += row_height
```

- [ ] **Step 3: Update `_make_plugin_row` to accept the new `desc_h` parameter.**

Find the signature around line 131-133:

```python
def _make_plugin_row(snap: PluginSnapshot, width: float, height: float,
                     *, credentials: dict[str, str],
                     client: DaemonClient, model: StatusModel) -> NSView:
```

Replace with:

```python
def _make_plugin_row(snap: PluginSnapshot, width: float, height: float,
                     *, desc_h: float = 32.0,
                     credentials: dict[str, str],
                     client: DaemonClient, model: StatusModel) -> NSView:
```

(Default of 32pt keeps the function callable from anywhere else that hasn't migrated; in practice `rebuild()` is the only caller.)

- [ ] **Step 4: Update the description-label frame in `_make_plugin_row`.**

Find the description block around line 143-149:

```python
    # Description label — 12pt secondary text, word-wrapped, ~32pt tall (2 lines).
    if snap.description:
        desc = NSTextField.labelWithString_(snap.description)
        desc.setFont_(typography.small())
        desc.setTextColor_(colors.text_secondary())
        desc.setLineBreakMode_(NSLineBreakByWordWrapping)
        desc.setFrame_(NSMakeRect(16, height - 60, width - 120, 32))
        row.addSubview_(desc)
```

Replace with:

```python
    # Description label — 12pt secondary text, word-wrapped. Height is
    # computed by the caller (rebuild() in build_plugins_tab) so the row
    # grows to fit instead of clipping at 2 lines.
    if snap.description:
        desc = NSTextField.labelWithString_(snap.description)
        desc.setFont_(typography.small())
        desc.setTextColor_(colors.text_secondary())
        desc.setLineBreakMode_(NSLineBreakByWordWrapping)
        # Description sits 28pt below the row top (height - 28 is the
        # name's baseline; the description bottom is height - 28 - desc_h).
        desc.setFrame_(NSMakeRect(16, height - 28 - desc_h,
                                  width - 120, desc_h))
        row.addSubview_(desc)
```

- [ ] **Step 5: Shift the interval and credentials block hardcoded y-coords down by `(desc_h - 32)`.**

Find the interval-row block around line 184-203 (it uses `height - 88`, `height - 92`, etc.):

```python
    if snap.kind == "scheduled":
        cfg = _config.load()
        override = cfg.interval_overrides.get(snap.id)
        seconds = override if override is not None else (snap.default_interval_s or 3600)
        initial_minutes = max(seconds // 60, 1)

        every_label = NSTextField.labelWithString_("Every")
        every_label.setFont_(typography.small())
        every_label.setFrame_(NSMakeRect(16, height - 88, 44, 16))
        row.addSubview_(every_label)

        interval_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(64, height - 92, 60, 22)
        )
```

The 88 / 92 numbers assume desc_h = 32; if desc_h grows, they need to grow too. Compute an `interval_y_base = 28 + desc_h + 28` (top + desc + interval-block-top-gap) and use it as a reference. Replace the block with:

```python
    if snap.kind == "scheduled":
        cfg = _config.load()
        override = cfg.interval_overrides.get(snap.id)
        seconds = override if override is not None else (snap.default_interval_s or 3600)
        initial_minutes = max(seconds // 60, 1)

        # Interval block sits below the description block. y_top is the
        # offset from the row's top edge (which is at y=height in AppKit
        # coords); we anchor relative to it so the block tracks the
        # description's actual rendered height.
        interval_y_top = height - 28 - desc_h - 28  # row top - name - desc - gap
        every_label = NSTextField.labelWithString_("Every")
        every_label.setFont_(typography.small())
        every_label.setFrame_(NSMakeRect(16, interval_y_top, 44, 16))
        row.addSubview_(every_label)

        interval_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(64, interval_y_top - 4, 60, 22)
        )
```

Continue updating the rest of the interval block: anywhere the file uses `height - 88` or `height - 92` or any constant assuming the old 32pt description, replace with the `interval_y_top` derived value. Read the file around lines 200-260 carefully to catch all of them.

- [ ] **Step 6: Verify Python syntax.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/preferences/plugins_tab.py').read())"
```

Expected: no output.

- [ ] **Step 7: Commit.**

```bash
git add packages/menubar/fulcra_menubar/preferences/plugins_tab.py
git commit -m "fix(menubar): grow Plugins-tab row to fit description (SP1 L2)

The Preferences Plugins tab allotted a fixed 32pt block for each
plugin's description, silently clipping anything past 2 lines. A
user reading e.g. \"Plex/Jellyfin webhook receiver — listens on
:8765 for scrobble webhooks from your media server. See docs for
setup\" never saw the docs reference.

Compute the description's actual word-wrapped height via
NSString.boundingRectWithSize_options_attributes_ (capped at 80pt,
~5 lines of small text), and grow the row accordingly. The
interval and credentials blocks shift down to follow.

Adds a _compute_desc_height helper near the top of the file and
threads a desc_h parameter through _make_plugin_row so the row can
position its descendants relative to the actual rendered height.

See SP1 L2 in the 2026-05-27 menubar drift audit."
```

---

## Task 4: Rebuild menubar app + manual verification

**File:** none modified — pure verification + optional follow-up commit if anything trailing needs cleanup.

**Why:** AppKit layout has no autonomous test path in this codebase (no XCUITest, no snapshot tests). The only way to verify SP1 is to rebuild the menubar binary and walk the surfaces visually.

- [ ] **Step 1: Reinstall the menubar tool with all extras and the workspace plugins.**

```bash
uv tool install --force --editable packages/menubar \
  --with-editable packages/collect \
  --with-editable packages/fulcra-common \
  --with-editable packages/attention \
  --with-editable packages/media-helpers \
  --with-editable packages/dayone \
  --with-editable packages/csv-importer \
  --with rumps --with pyobjc-core \
  --with pyobjc-framework-Cocoa --with pyobjc-framework-UserNotifications \
  --with pyobjc-framework-ServiceManagement --with pyobjc-framework-Quartz 2>&1 | tail -5
```

Expected: `Installed 1 executable: fulcra-menubar`.

- [ ] **Step 2: Stop the currently-running menubar instance.**

```bash
pkill -f fulcra-menubar 2>/dev/null
sleep 1
ps aux | grep -i "fulcra-menubar\|fulcra_menubar" | grep -v grep
```

Expected: no output from the second `ps`.

- [ ] **Step 3: Relaunch the menubar app in the background.**

```bash
fulcra-menubar 2>&1 &
sleep 3
ps aux | grep -i "fulcra-menubar\|fulcra_menubar" | grep -v grep
```

Expected: the second `ps` lists exactly one running process. Capture the PID.

- [ ] **Step 4: Update the running-process memory file.**

Per `~/.claude/CLAUDE.md`'s "Record long-running processes I start" rule, update `/Users/Scanning/.claude/projects/-Users-Scanning-Developer-fulcra-tools/memory/project_fulcra_menubar_running.md` with the new PID.

- [ ] **Step 5: Surface the manual walkthrough to the user.**

The user runs:

1. Click the menubar icon → opens the popover. Find the **Quick Record** band. Find a Duration-kind row (e.g. Attention, or a Listened/Watched track). Visually confirm:
   - The comment field is slightly narrower than before.
   - The Record button and the Start/Stop button now have a noticeable gap between them (24pt instead of 8pt).
   - The row still aligns vertically (name + comment + duration + Record + Timer all on a coherent line).

2. Open Preferences from the popover gear icon. Switch to the **Plugins tab**. Scroll through the plugins. Confirm:
   - Long descriptions (look for Plex/Jellyfin or Apple Podcasts which have multi-line descriptions) now render in full instead of being clipped at 2 lines.
   - The interval label / field / credentials block shifts down to follow each description's actual height.
   - Short descriptions (e.g. one-liner plugins) still look fine — the row just grows for descriptions that need it.

3. Switch to the **About tab**. Confirm:
   - The "Launch at login" label/switch/caption sit with comfortable vertical breathing above the separator.
   - The separator-to-first-identity-row gap looks generous (~24pt; was very tight before).
   - The plugin-versions scrollable list at the bottom still has plenty of room.

- [ ] **Step 6: Pre-push orphan/obsolete sweep.**

Per `~/.claude/CLAUDE.md`, before any git push:

```bash
git diff 41b29ee..HEAD --stat
git diff 41b29ee..HEAD -- packages/menubar/
```

Read the diff for: stale comments referring to "32pt description block" or "8pt gap" or other pre-fix language; unused imports introduced by Task 3; pre-existing hardcoded y-coords in `plugins_tab.py` that became wrong after the row's height changed.

If you find anything, commit a follow-up:

```bash
git add packages/menubar/
git commit -m "chore(menubar): orphan/obsolete sweep after SP1 (SP1 follow-up)

[describe what you found and fixed]"
```

---

## Acceptance Checklist

- [ ] About-tab top action row has visibly increased vertical breathing (~24pt) between the caption and the separator AND between the separator and the first identity row.
- [ ] Quick-record Duration row's Record and Timer buttons have a clear gap (~24pt) — eyeballable as "obvious clearance" rather than "buttons touching".
- [ ] Plugins-tab descriptions render in full when they exceed 2 lines, up to the 80pt cap. Short descriptions still look fine.
- [ ] No Python syntax errors in any of the three modified files (`python3 -c "import ast; ast.parse(...)"` is the gate).
- [ ] Menubar app launches without crashes after the rebuild.
- [ ] No regression in pre-existing behaviour (the existing functionality of each surface is unchanged — only the spacing/sizing).
- [ ] Pre-push orphan/obsolete sweep clean (or any findings committed as a follow-up).

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| The description-height computation in Task 3 returns a wrong size for some plugin, breaking the row layout. | Cap at 80pt protects against runaway sizes. Manual walkthrough in Task 4 Step 5 will catch any visible regression — every plugin row will be eyeballed during the Plugins-tab scan. |
| Imports needed by Task 3 (NSString, boundingRectWithSize_options_attributes_, NSStringDrawingUsesLineFragmentOrigin) aren't already present in the file. | Implementer Step 1 explicitly notes "check the existing imports first; add only what's missing." |
| The hardcoded y-coords in plugins_tab.py beyond the interval block (credentials section) may also assume `desc_h = 32` and need updating. | Task 3 Step 5 explicitly instructs reading lines 200-260 carefully to catch all `height - N` constants that depend on the old description height. |
| User has the menubar app open during the rebuild — the new binary won't show changes until relaunch. | Task 4 Step 2 explicitly kills any running instance before Step 3 relaunches. |
