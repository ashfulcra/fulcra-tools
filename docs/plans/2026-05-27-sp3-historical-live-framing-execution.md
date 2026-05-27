# SP3: Historical/live framing + pill mapping unification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Surface the historical-vs-live framing the web UI's `collect_modes` onboarding screen introduced in the menubar's plugin views, and unify the popover's status-dot mapping with the dashboard's three-tier pill so the icon-badge layer, the popover dot, and the web UI all agree about what "1 failure" vs "3+ failures" means.

**Architecture:** Add an explicit `collect_mode: Literal["historical", "live_polled", "live_continuous"]` field to the Plugin contract. Every plugin module declares its value explicitly (no derive-from-kind — the `manual` kind covers BOTH historical takeouts AND the Attention extension, which is functionally live_continuous despite the `manual` kind). Propagate through the daemon's status reply. Menubar's `PluginSnapshot` gains the field. The popover plugin-status view re-groups by `collect_mode` instead of by `kind`. Preferences → Plugins gets a small per-row chip. The popover's status-dot mapping is rewritten to match `dashboard.js`'s three-tier pill.

**Tech Stack:** Python 3.12+ (Plugin contract, daemon), PyObjC + rumps (menubar).

**Source spec:** `docs/plans/2026-05-27-menubar-vs-webui-drift-scope.md` §SP3 (drift items D4 + D5). User Q2 answer: "Augment — hist/live in popover, kind in Prefs". HEAD at plan start: `6062f56` (end of SP2).

**Reading list before starting:**
- `packages/collect/fulcra_collect/plugin.py:145-217` — Plugin dataclass; add new field after `category`.
- `packages/web-ui/dist/static/dashboard.js:70-104` — the three-tier pill mapping the popover's status dot must match.
- `packages/menubar/fulcra_menubar/popover/plugin_row.py` — `_status_dot()` (the current coarser two-tier mapping).
- `packages/menubar/fulcra_menubar/popover/root.py` — where the plugin-status view groups by `kind` today.
- `packages/menubar/fulcra_menubar/model.py` — `PluginSnapshot` field to add.
- Every plugin module under `packages/media-helpers/fulcra_media/plugins/*.py`, `packages/attention/fulcra_attention/collect_plugin.py`, `packages/dayone/fulcra_dayone/collect_plugin.py` — 18 files total, one new `collect_mode=` per file.

---

## File Structure

| File | Change | Why |
|---|---|---|
| `packages/collect/fulcra_collect/plugin.py` | Modify | Add `CollectMode` Literal + `collect_mode` field on `Plugin` dataclass (required, no default — every plugin declares explicitly). |
| Every plugin module (18 files) | Modify | Add `collect_mode=...` to its Plugin instantiation. |
| `packages/collect/fulcra_collect/routes/status.py` | Modify | Include `collect_mode` in each plugin entry of `/api/status` reply. |
| `packages/collect/fulcra_collect/daemon.py` | Modify | Same, in the UDS `status` reply path. |
| `packages/collect/tests/test_plugin_contract.py` (or wherever) | Modify | Assert the new field is in the dataclass + the validation rejects unknown values. |
| `packages/menubar/fulcra_menubar/model.py` | Modify | Add `collect_mode: str` to `PluginSnapshot`. |
| `packages/menubar/fulcra_menubar/popover/root.py` | Modify | Group plugin-status view by `collect_mode` instead of `kind`. |
| `packages/menubar/fulcra_menubar/popover/plugin_row.py` | Modify | Rewrite `_status_dot` mapping to match the dashboard's three-tier pill. |
| `packages/menubar/fulcra_menubar/preferences/plugins_tab.py` | Modify | Add small `collect_mode` chip per row (kind taxonomy stays — per Q2 user answer). |

Total surface: ~30 plugin-module one-line additions + ~80 menubar lines + ~40 daemon-side lines + ~30 test lines.

---

## Plugin → collect_mode mapping (locked in)

Reviewed every plugin's actual data-flow semantics, not just its `kind`:

| Plugin | kind | collect_mode | rationale |
|---|---|---|---|
| `apple_music_takeout` | manual | historical | one-shot zip import |
| `apple_podcasts` | scheduled | live_polled | polls finished-episode DB every 6h |
| `apple_podcasts_timemachine` | manual | historical | recovers from older TM snapshots |
| `apple_takeout` | manual | historical | one-shot zip import |
| `attention-relay` | manual | live_continuous | extension pushes via webhook; `run()` is a status no-op |
| `dayone` | scheduled | live_polled | polls journal DB every 6h |
| `deezer` | scheduled | live_polled | polls API |
| `generic_csv` | manual | historical | one-shot user-provided file |
| `generic_rss` | scheduled | live_polled | polls feed |
| `goodreads` | scheduled | live_polled | polls 'read' shelf feed |
| `lastfm` | scheduled | live_polled | polls scrobbles |
| `letterboxd` | scheduled | live_polled | polls feed |
| `media_webhook` | service | live_continuous | receives Plex/Jellyfin pushes |
| `netflix` | manual | historical | one-shot takeout |
| `spotify_extended` | manual | historical | one-shot zip import |
| `spotify_ifttt` | manual | historical | one-shot user-provided file via IFTTT |
| `trakt` | scheduled | live_polled | polls API |
| `youtube` | manual | historical | one-shot takeout |

**Note on `attention-relay`:** the only "manual" → "live_continuous" override. The extension pushes events directly; the `run()` callable is a no-op status check, but the functional data flow is continuous live capture. The mapping is per-plugin-explicit precisely so cases like this can be expressed correctly.

---

## Task 1: Plugin contract — add `collect_mode` field + populate every plugin

**Files:**
- Modify: `packages/collect/fulcra_collect/plugin.py:145-217`
- Modify: every plugin module (18 files, see mapping table above)
- Modify: relevant tests (find `test_plugin_contract.py` or similar)

**Step 1: Write a failing test asserting the new field exists + validates.**

In `packages/collect/tests/` (probably `test_plugin_contract.py`, or create one if missing), add:

```python
def test_plugin_requires_collect_mode():
    """Every plugin must declare collect_mode — historical / live_polled /
    live_continuous. The mapping isn't derivable from `kind` because the
    Attention extension's kind='manual' but functionally collect_mode=
    'live_continuous'. Forcing per-plugin explicit values surfaces the
    distinction at the metadata level."""
    from fulcra_collect.plugin import Plugin
    # Construct a Plugin without collect_mode → should fail.
    with pytest.raises(TypeError):  # dataclass missing required field
        Plugin(
            id="test",
            name="Test",
            kind="manual",
            run=lambda ctx: None,
        )


def test_plugin_rejects_unknown_collect_mode():
    """collect_mode must be one of the three known values."""
    from fulcra_collect.plugin import Plugin
    with pytest.raises(ValueError, match="unknown collect_mode"):
        Plugin(
            id="test",
            name="Test",
            kind="manual",
            collect_mode="bogus",
            run=lambda ctx: None,
        )


def test_plugin_accepts_known_collect_modes():
    """All three values must construct without error."""
    from fulcra_collect.plugin import Plugin
    for mode in ("historical", "live_polled", "live_continuous"):
        p = Plugin(
            id=f"test-{mode}",
            name="Test",
            kind="manual",
            collect_mode=mode,
            run=lambda ctx: None,
        )
        assert p.collect_mode == mode
```

If `test_plugin_contract.py` doesn't exist, look for the closest existing contract test (grep for `test_plugin`, `Plugin(` instantiations in tests). If nothing's close, create the file at `packages/collect/tests/test_plugin_contract.py`.

**Step 2: Run the failing test.**

```bash
cd packages/collect && uv run pytest tests/test_plugin_contract.py -v
```

Expected: all 3 tests FAIL — the field doesn't exist yet.

**Step 3: Add the field to the Plugin dataclass.**

In `packages/collect/fulcra_collect/plugin.py`, add after the `PluginKind = Literal["service", "scheduled", "manual"]` line (around line 16):

```python
CollectMode = Literal["historical", "live_polled", "live_continuous"]
_COLLECT_MODES = ("historical", "live_polled", "live_continuous")
```

In the `Plugin` dataclass (around line 165, after `kind: PluginKind`), add:

```python
    collect_mode: CollectMode
    """Per-plugin tag for the user-facing 'historical vs live' framing the
    web UI's collect_modes onboarding screen introduced. Three values:

      "historical"      — one-shot import; the plugin doesn't update
                          afterwards (takeouts, user-provided files).
      "live_polled"     — captures new events on a polling schedule.
      "live_continuous" — captures events as they happen (webhook
                          receivers, browser-extension pushes).

    NOT derivable from `kind` — the Attention extension's kind="manual"
    but functionally collect_mode="live_continuous" because the data
    flow is push-based via the extension. Forcing per-plugin explicit
    values surfaces this distinction at the metadata level. See SP3
    in the 2026-05-27 menubar drift audit for the full mapping table.
    """
```

In `__post_init__`, add:

```python
        if self.collect_mode not in _COLLECT_MODES:
            raise ValueError(
                f"unknown collect_mode {self.collect_mode!r}; "
                f"expected one of {_COLLECT_MODES}"
            )
```

**Step 4: Add `collect_mode=` to every plugin module.**

For each of the 18 plugins in the mapping table above, find the `Plugin(...)` instantiation and add the corresponding `collect_mode=` argument. Place it after `kind=` for visual consistency.

Files + values:

```
packages/media-helpers/fulcra_media/plugins/apple_music_takeout.py        → collect_mode="historical"
packages/media-helpers/fulcra_media/plugins/apple_podcasts.py             → collect_mode="live_polled"
packages/media-helpers/fulcra_media/plugins/apple_podcasts_timemachine.py → collect_mode="historical"
packages/media-helpers/fulcra_media/plugins/apple_takeout.py              → collect_mode="historical"
packages/media-helpers/fulcra_media/plugins/deezer.py                     → collect_mode="live_polled"
packages/media-helpers/fulcra_media/plugins/generic_csv.py                → collect_mode="historical"
packages/media-helpers/fulcra_media/plugins/generic_rss.py                → collect_mode="live_polled"
packages/media-helpers/fulcra_media/plugins/goodreads.py                  → collect_mode="live_polled"
packages/media-helpers/fulcra_media/plugins/lastfm.py                     → collect_mode="live_polled"
packages/media-helpers/fulcra_media/plugins/letterboxd.py                 → collect_mode="live_polled"
packages/media-helpers/fulcra_media/plugins/media_webhook.py              → collect_mode="live_continuous"
packages/media-helpers/fulcra_media/plugins/netflix.py                    → collect_mode="historical"
packages/media-helpers/fulcra_media/plugins/spotify_extended.py           → collect_mode="historical"
packages/media-helpers/fulcra_media/plugins/spotify_ifttt.py              → collect_mode="historical"
packages/media-helpers/fulcra_media/plugins/trakt.py                      → collect_mode="live_polled"
packages/media-helpers/fulcra_media/plugins/youtube.py                    → collect_mode="historical"
packages/dayone/fulcra_dayone/collect_plugin.py                           → collect_mode="live_polled"
packages/attention/fulcra_attention/collect_plugin.py                     → collect_mode="live_continuous"
```

Note the Attention deviation — `kind="manual"` but `collect_mode="live_continuous"`.

**Step 5: Run the test + the full collect suite.**

```bash
cd packages/collect && uv run pytest tests/test_plugin_contract.py -v
cd packages/collect && uv run pytest -q 2>&1 | tail -3
```

Expected: 3 plugin-contract tests PASS, full collect suite passes at 366+ (was 1447 before SP3; +3 from this task ≈ 1450 across workspace).

**Step 6: Commit.**

```bash
git add packages/collect/fulcra_collect/plugin.py \
       packages/collect/tests/test_plugin_contract.py \
       packages/media-helpers/fulcra_media/plugins/*.py \
       packages/dayone/fulcra_dayone/collect_plugin.py \
       packages/attention/fulcra_attention/collect_plugin.py
git commit -m "feat(collect): add per-plugin collect_mode field for SP3 framing (SP3 task 1)

Adds a required collect_mode field on the Plugin dataclass:
  - historical: one-shot import (takeouts)
  - live_polled: scheduled polling (Last.fm, Trakt, etc.)
  - live_continuous: push-based (webhook receivers, Attention extension)

Every existing plugin (18 modules across media-helpers, dayone,
attention) gets an explicit value. NOT derivable from kind: the
Attention extension's kind=\"manual\" but collect_mode=
\"live_continuous\" because the data flow is push-based via the
extension. Forcing per-plugin explicit values surfaces this at the
metadata level.

3 new contract tests cover the required-field, unknown-value,
known-values cases.

The menubar UI changes that consume this (popover regroup,
Preferences chip, status-dot pill unification) land in subsequent
SP3 task commits. The daemon status-reply propagation lands in
the next commit.

Refs SP3 D4 + D5, drift audit 2026-05-27."
```

---

## Task 2: Daemon status-reply propagation

**Files:**
- Modify: `packages/collect/fulcra_collect/routes/status.py` (HTTP `/api/status`)
- Modify: `packages/collect/fulcra_collect/daemon.py` (UDS `status` command — `handle_request` branch)

**Why:** Both surfaces enumerate plugins. Add `collect_mode` to each plugin entry so the menubar (UDS) and any future web-UI consumer (HTTP) can read it.

- [ ] **Step 1: Update HTTP `/api/status` response shape.**

Open `packages/collect/fulcra_collect/routes/status.py`. Find the loop that builds the per-plugin response dict. Add `"collect_mode": p.collect_mode` alongside the existing `"kind": p.kind`.

- [ ] **Step 2: Update UDS `status` response shape (daemon.py).**

Open `packages/collect/fulcra_collect/daemon.py`. Find `handle_request`'s `if cmd == "status":` branch (around line 388-410) and follow the call chain to wherever the per-plugin dict is built. Add `"collect_mode": p.collect_mode` to that dict.

- [ ] **Step 3: Update or add a test asserting the new field in the reply.**

Look for existing status-reply tests (`grep -rn "collect_mode\|api/status\|cmd.*status" packages/collect/tests/`). Add an assertion that the new field appears in each plugin entry with the correct value.

- [ ] **Step 4: Run tests.**

```bash
cd packages/collect && uv run pytest -q
```

Expected: no regressions; new test (if added) passes.

- [ ] **Step 5: Commit.**

```bash
git add packages/collect/fulcra_collect/routes/status.py \
       packages/collect/fulcra_collect/daemon.py \
       packages/collect/tests/
git commit -m "feat(collect): propagate collect_mode through status replies (SP3 task 2)

Both the HTTP /api/status route and the UDS \`status\` command now
include each plugin's collect_mode value alongside its kind. The
menubar consumes this in SP3 tasks 3-4 to drive the popover's new
historical-vs-live grouping and a per-row chip in Preferences.

Refs SP3 D5, drift audit 2026-05-27."
```

---

## Task 3: Menubar PluginSnapshot field + popover regroup

**Files:**
- Modify: `packages/menubar/fulcra_menubar/model.py` (PluginSnapshot)
- Modify: `packages/menubar/fulcra_menubar/popover/root.py` (group-by-kind → group-by-collect_mode)

- [ ] **Step 1: Add `collect_mode` to `PluginSnapshot`.**

Find the `PluginSnapshot` dataclass (likely a `@dataclass` or `NamedTuple` in `model.py`). Add a `collect_mode: str` field. Update wherever `PluginSnapshot` is constructed from the daemon's status reply — the field should be populated from the new daemon-side `collect_mode` field.

- [ ] **Step 2: Replace the popover's group-by-kind logic.**

Find the grouping logic in `popover/root.py` (likely `_status_container` or equivalent — where today's plugin-status view lists plugins under `service / scheduled / manual` headers). Rewrite to group by `collect_mode`:

| collect_mode | section header |
|---|---|
| `live_continuous` | "Live (continuous)" |
| `live_polled` | "Live (polled)" |
| `historical` | "Historical (one-shot)" |

Group order in the rendered list: continuous → polled → historical (most-live first).

Update or add comments explaining the rationale + the SP3 reference.

- [ ] **Step 3: Verify Python syntax.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/model.py').read())"
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/popover/root.py').read())"
```

- [ ] **Step 4: Run menubar pytest.**

```bash
cd packages/menubar && uv run pytest -q
```

Expected: 106 passing (baseline). If `PluginSnapshot`-construction tests exist they may need updating to include the new field.

- [ ] **Step 5: Commit.**

```bash
git add packages/menubar/fulcra_menubar/model.py \
       packages/menubar/fulcra_menubar/popover/root.py
git commit -m "feat(menubar): PluginSnapshot.collect_mode + popover regroup by it (SP3 task 3)

The popover plugin-status view used to group plugins under
'service / scheduled / manual' headers — that's the technical
taxonomy from the Plugin contract's kind field. SP3's drift fix:
re-group by collect_mode instead, matching the historical-vs-live
framing the web UI's collect_modes onboarding screen introduced.

Three groups in the rendered list:
  Live (continuous) — push-based (Plex webhook, Attention extension)
  Live (polled)     — scheduled polling (Last.fm, Trakt, …)
  Historical (one-shot) — takeouts and user-provided files

The kind taxonomy stays in Preferences → Plugins per user Q2:
the technical truth matters there for Run-now affordances + the
default scheduling interval. The popover, where the user wants
'is it flowing?' at a glance, uses the user-facing framing.

Refs SP3 D5, drift audit 2026-05-27."
```

---

## Task 4: Pill mapping unification + Preferences chip

**Files:**
- Modify: `packages/menubar/fulcra_menubar/popover/plugin_row.py` (`_status_dot`)
- Modify: `packages/menubar/fulcra_menubar/preferences/plugins_tab.py` (collect_mode chip per row)

**Why:** Two distinct fixes bundled because both are small and live in the same neighbourhood.

(4a) Current `_status_dot` uses a coarser two-tier mapping (red on ANY failure, amber on running, mint else, gray on disabled). The dashboard at `packages/web-ui/dist/static/dashboard.js:70-104` uses a richer three-tier mapping: red on ≥3 consecutive failures, amber on 1-2 failures ("Failed — run again"), violet on running, mint on healthy, slate on not-run-yet, gray on disabled. Make the popover dot match.

(4b) Per Q2 the kind taxonomy stays in Preferences but a small `collect_mode` chip on each row tells the user which is which.

- [ ] **Step 1: Read dashboard.js's pill-mapping function.**

```bash
sed -n '60,110p' packages/web-ui/dist/static/dashboard.js
```

Capture the EXACT classification (especially the 1-2 vs ≥3 consecutive-failures threshold) so the menubar mirrors it.

- [ ] **Step 2: Rewrite `_status_dot` in plugin_row.py.**

Find the function. Replace its body with a three-tier mapping that matches dashboard.js. Document each branch with the same human-readable label the dashboard uses ("Failing", "Failed — run again", "Running", "Healthy", "Not run yet", "Disabled").

- [ ] **Step 3: Add `collect_mode` chip to Preferences → Plugins rows.**

In `plugins_tab.py`'s `_make_plugin_row`, after the row's name + description rendering, add a small chip (NSTextField with a thin border + small font) displaying the collect_mode value. Map to human-readable text: `"historical"` → "Historical", `"live_polled"` → "Live (polled)", `"live_continuous"` → "Live (continuous)".

Place it near the kind label / interval block so the two coexist visibly (kind = technical, chip = framing — per Q2 "augment, not replace").

- [ ] **Step 4: Verify syntax + pytest.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/popover/plugin_row.py').read())"
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/preferences/plugins_tab.py').read())"
cd packages/menubar && uv run pytest -q
```

- [ ] **Step 5: Commit.**

```bash
git add packages/menubar/fulcra_menubar/popover/plugin_row.py \
       packages/menubar/fulcra_menubar/preferences/plugins_tab.py
git commit -m "feat(menubar): three-tier status dot + Preferences collect_mode chip (SP3 task 4)

Two fixes bundled, both small and in the same neighbourhood.

(D4) Popover plugin_row's _status_dot used a coarse two-tier
mapping (red on any failure, amber on running, mint else,
gray on disabled). The dashboard's pill is richer (red on ≥3
consecutive failures, amber on 1-2 'Failed — run again',
violet on running, mint on healthy, slate on not-run-yet,
gray on disabled). Make the popover match. Internally consistent
with the menubar icon's badge mapping (status_item.py:130-137)
which also uses the three-tier model.

(D5) Preferences → Plugins gets a per-row collect_mode chip
showing 'Historical' / 'Live (polled)' / 'Live (continuous)'.
The kind taxonomy stays per user Q2 — chip augments rather
than replacing.

Refs SP3 D4 + D5, drift audit 2026-05-27."
```

---

## Task 5: Rebuild menubar + manual verification

**Files:** none modified — verification + optional orphan-sweep follow-up.

- [ ] **Step 1: Reinstall menubar (same incantation as SP1/SP2 Task 5).**

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
  --with pyobjc-framework-ServiceManagement --with pyobjc-framework-Quartz 2>&1 | tail -3
```

- [ ] **Step 2: Restart daemon + menubar.**

```bash
launchctl kickstart -k gui/$(id -u)/com.fulcra.collect
sleep 3
pkill -f fulcra-menubar 2>/dev/null
sleep 1
fulcra-menubar 2>&1 >/dev/null &
disown
sleep 4
ps aux | grep -E "fulcra-menubar|com\.fulcra\.collect" | grep -v grep | head -4
```

- [ ] **Step 3: Update running-process memory file.**

- [ ] **Step 4: Full pytest sweep.**

```bash
uv run --all-packages pytest -q packages/ 2>&1 | tail -3
```

Expected: 1450+ passed (1447 SP2 baseline + 3 from SP3 task 1 + maybe a few from task 2).

- [ ] **Step 5: Smoke-test the daemon's status reply via curl.**

```bash
TOKEN=$(cat ~/.config/fulcra-collect/web-token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9292/api/status \
  | python3 -c "import sys, json; d = json.load(sys.stdin); plugins = d.get('plugins', []); print('count:', len(plugins)); modes = {p.get('id'): p.get('collect_mode') for p in plugins}; [print(f'  {k}: {v}') for k, v in sorted(modes.items())]"
```

Expected: each plugin has the right `collect_mode` value per the SP3 mapping table.

- [ ] **Step 6: Orphan/obsolete sweep.**

```bash
git diff 6062f56..HEAD --stat
grep -rn "group by kind\|service / scheduled / manual\b" packages/menubar/
```

Update any stale comments / dead references the SP3 changes orphaned.

- [ ] **Step 7: Surface manual walkthrough to user.**

User verifies:

A. **Popover plugin-status view (click "View Status →" in popover):**
- Plugins now grouped under "Live (continuous)" / "Live (polled)" / "Historical (one-shot)" headers.
- Order: continuous → polled → historical.
- attention-relay sits under "Live (continuous)" (NOT under historical, despite kind="manual").
- Status dots:
  - A plugin with 3+ consecutive failures shows RED.
  - A plugin with 1-2 failures shows AMBER ("Failed — run again" semantics — matches the web UI dashboard pill).
  - Running plugins show VIOLET.
  - Healthy plugins show MINT.
  - Not-run-yet plugins show SLATE.
  - Disabled shows GRAY.

B. **Preferences → Plugins tab:**
- Each row still grouped by kind (service / scheduled / manual — unchanged).
- Each row has a small chip showing "Historical" / "Live (polled)" / "Live (continuous)".
- The chip placement doesn't break SP1 L2 (the dynamic description height).

C. **No regressions:**
- SP1 + SP2 surfaces still work as before.

---

## Final cross-cutting code review

After all 5 tasks land, dispatch `superpowers:code-reviewer` over the combined diff `6062f56..HEAD`. Focus areas:
- Daemon status reply contract change: does any non-menubar consumer break? (Web UI dashboard probably doesn't read `collect_mode`; the field is additive so should be safe.)
- Every plugin has a `collect_mode` value AND the mapping is consistent with the table.
- Popover regroup doesn't break edge cases (e.g., no plugins in a group; no plugins enabled at all).
- Status-dot pill mapping byte-matches the dashboard's mapping for every state.

## Acceptance

- [ ] `Plugin` dataclass requires `collect_mode`; every plugin module sets one.
- [ ] HTTP `/api/status` + UDS `status` both include `collect_mode` per plugin.
- [ ] Menubar `PluginSnapshot.collect_mode` propagates through.
- [ ] Popover groups by collect_mode with three sections.
- [ ] Popover status dot uses three-tier mapping matching dashboard.
- [ ] Preferences → Plugins keeps kind taxonomy, adds chip.
- [ ] Manual walkthrough passed.
