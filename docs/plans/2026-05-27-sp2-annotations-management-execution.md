# SP2: Annotation-management Preferences tab + popover delete — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Bring soft-delete + bulk favorites management into the menubar so users don't context-switch to the web UI Settings page for either. Closes drift items D1 + D2 from the 2026-05-27 menubar drift audit.

**Architecture:** Three layers of change. (1) Daemon refactor: extract the existing `delete_definition` HTTP route's business logic into a reusable `Daemon._delete_definition(def_id)` method, then expose it via a new UDS command so non-HTTP callers (the menubar) can trigger it. (2) DaemonClient (menubar side) gets a new `delete_definition` method. (3) Menubar UI gains: a new "Annotations" Preferences tab with bulk favorites + soft-delete per-row, plus a "..." per-row menu in the popover quick-record list with a "Delete this track…" item.

**Tech Stack:** Python (FastAPI, pytest on the daemon side; PyObjC + rumps + AppKit on the menubar side).

**Source spec:** `docs/plans/2026-05-27-menubar-vs-webui-drift-scope.md` §5 SP2 (drift items D1 + D2). User-locked Q1 (new Annotations tab + per-row "…" menu in popover) and Q4 (simple NSAlert confirmation). HEAD at plan start: `4fb591e` (end of SP1).

**Reading list before starting:**
- `packages/collect/fulcra_collect/routes/definitions.py:224-338` — the existing HTTP route whose body Task 1 extracts.
- `packages/collect/fulcra_collect/daemon.py:388-410` — the existing UDS-command dispatch where Task 1 plugs in.
- `packages/menubar/fulcra_menubar/daemon_client.py:127` — sibling method (`delete_annotation`) Task 2's new method mirrors.
- `packages/menubar/fulcra_menubar/preferences/window.py` — tab registration target for Task 3.
- `packages/menubar/fulcra_menubar/preferences/notifications_tab.py` — simplest tab template Task 3 can mirror in shape.
- `packages/menubar/fulcra_menubar/popover/quick_record.py:574-735` — the row builders Task 4 augments.
- `packages/web-ui/dist/static/settings.js` — the reference behaviour the menubar mirrors.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `packages/collect/fulcra_collect/daemon.py` | Modify | Gains `_delete_definition(def_id, fulcra_token)` method (extracted from the HTTP route) and a new `delete_definition` UDS-command branch in `handle_request`. |
| `packages/collect/fulcra_collect/routes/definitions.py` | Modify | The existing HTTP route delegates to `daemon._delete_definition` — no behavioural change. |
| `packages/collect/tests/test_daemon_delete_definition.py` | Create | Unit test for the new UDS command path. |
| `packages/menubar/fulcra_menubar/daemon_client.py` | Modify | New `delete_definition(def_id)` method sending the new UDS command. |
| `packages/menubar/fulcra_menubar/preferences/annotations_tab.py` | Create | New "Annotations" Preferences tab — scrollable list of defs with favorites checkbox + Delete button per row. |
| `packages/menubar/fulcra_menubar/preferences/window.py` | Modify | Register the new tab between Plugins and Notifications. |
| `packages/menubar/fulcra_menubar/popover/quick_record.py` | Modify | Add a "…" per-row menu button with a Delete-this-track item. |

Total surface: ~250 lines added, ~50 modified. One new daemon command, one new menubar method, one new file, ~70 lines of incidental UI changes.

---

## Task 1: Daemon-side — extract delete_definition + add UDS command

**Files:**
- Modify: `packages/collect/fulcra_collect/daemon.py`
- Modify: `packages/collect/fulcra_collect/routes/definitions.py:224-338`
- Create: `packages/collect/tests/test_daemon_delete_definition.py`

**Why:** The HTTP route already does the right thing (calls Fulcra to soft-delete, clears plugin state, prunes favorites). The menubar can't call HTTP routes directly via UDS — it goes through the daemon's `handle_request` command surface. We refactor the HTTP route's body into a Daemon method (which both surfaces share) rather than duplicate the logic.

- [ ] **Step 1: Write the failing test.**

Create `packages/collect/tests/test_daemon_delete_definition.py`:

```python
"""Daemon UDS command `delete_definition` — mirrors the HTTP route
behaviour (clears plugin state, prunes favorites) but is reachable
via the local UDS socket so the menubar can call it.

Why this test exists: the menubar's preferences/annotations_tab.py
(SP2 Task 3) and popover quick-record "…" menu (SP2 Task 4) both
trigger soft-delete from non-HTTP surfaces. The shared Daemon method
introduced in this task is the single business-logic site; the HTTP
route delegates to it (so the existing HTTP-side tests should still
pass without modification).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_delete_definition_uds_command(daemon_with_fake_fulcra) -> None:
    """The handle_request branch routes to _delete_definition."""
    daemon, fake_fulcra = daemon_with_fake_fulcra
    # The daemon should accept the new UDS command and call out to Fulcra.
    fake_fulcra.set_delete_response(204)
    response = daemon.handle_request({
        "cmd": "delete_definition",
        "def_id": "fake-uuid-1234",
    })
    assert response.get("ok") is True
    assert "cleared_plugins" in response


def test_delete_definition_uds_command_missing_def_id(
    daemon_with_fake_fulcra,
) -> None:
    """Missing def_id is a client-side error, returned as ok=False."""
    daemon, _ = daemon_with_fake_fulcra
    response = daemon.handle_request({"cmd": "delete_definition"})
    assert response.get("ok") is False
    assert "def_id" in response.get("error", "").lower()


def test_delete_definition_uds_command_fulcra_404(
    daemon_with_fake_fulcra,
) -> None:
    """When Fulcra returns 404, the UDS response surfaces it gracefully."""
    daemon, fake_fulcra = daemon_with_fake_fulcra
    fake_fulcra.set_delete_response(404)
    response = daemon.handle_request({
        "cmd": "delete_definition",
        "def_id": "nonexistent-uuid",
    })
    assert response.get("ok") is False
    assert "not found" in response.get("error", "").lower()
```

You'll need a `daemon_with_fake_fulcra` fixture. Check whether existing daemon-test files already build one (`grep -rn "daemon_with_fake_fulcra\|FakeFulcra" packages/collect/tests/`). If yes, lift its conftest entry; if no, inline-build the fixture at the top of `test_daemon_delete_definition.py` — pattern: construct a `Daemon` against a temp `collect_home`, monkey-patch `web.httpx` to return a `FakeFulcra` stub that exposes `set_delete_response(status_code)`.

- [ ] **Step 2: Run the failing test.**

```bash
cd packages/collect && uv run pytest tests/test_daemon_delete_definition.py -v
```

Expected: 3 FAILED with `KeyError: 'delete_definition'` or `AttributeError` on the `cmd` branch — the daemon doesn't know the new UDS command yet.

- [ ] **Step 3: Extract HTTP-route body into `Daemon._delete_definition`.**

In `packages/collect/fulcra_collect/daemon.py`, add a new method to the `Daemon` class (after `_set_quick_record_favorites` and friends, before `handle_request`). The body is the current HTTP route's logic from `routes/definitions.py:235-338`, minus the FastAPI-specific `HTTPException` raises (which become structured `{"ok": False, "error": "..."}` returns).

```python
def _delete_definition(self, def_id: str) -> dict:
    """Soft-delete an annotation definition via Fulcra, then clean up
    locally — clear any plugin state bound to it, prune from favorites.

    Single business-logic site shared by the HTTP route (still in
    routes/definitions.py, now a thin wrapper) and the UDS command
    (added in handle_request below). Returns the same shape as the
    HTTP route did on success: {"ok": True, "cleared_plugins": [ids]}.
    Returns {"ok": False, "error": "..."} on any failure mode the HTTP
    route used to raise HTTPException for.

    Args:
        def_id: the annotation definition UUID to soft-delete.
    """
    from . import web as _web  # late import — tests monkeypatch web.httpx

    _log = logging.getLogger("fulcra_collect.daemon")
    if not def_id:
        return {"ok": False, "error": "def_id is required"}

    fulcra_token = self._get_fulcra_token()
    if not fulcra_token:
        return {"ok": False, "error": "not signed in to Fulcra"}

    # ... [body extracted from routes/definitions.py:243-291, with
    #      HTTPException replaced by {"ok": False, "error": ...} returns] ...

    # ... [the post-delete cleanup is unchanged: walk plugins to
    #      clear cached definition_id, prune from favorites, bust
    #      the quick-record cache] ...

    return {"ok": True, "cleared_plugins": cleared}
```

Replace `[body extracted ...]` with the actual lines from `routes/definitions.py:243-338`. Translate each `raise HTTPException(status, msg)` to `return {"ok": False, "error": msg}`. The cleanup section (walk plugins, prune favorites, bust cache) lifts in directly.

You'll need `self._get_fulcra_token()` — check whether that helper exists on Daemon; if not, look at how the HTTP route's `fulcra_token_or_401` works (it's a closure-injected dependency at `routes/definitions.py:238`) and provide an equivalent. The HTTP route's token comes from the request's Fulcra-Authorization header or the keychain; for the UDS path, only the keychain matters (the menubar is a local process).

- [ ] **Step 4: Add the UDS-command branch in `handle_request`.**

In `packages/collect/fulcra_collect/daemon.py`, in the `handle_request` method, find the block around line 388-410 with the other UDS commands. Add:

```python
        if cmd == "delete_definition":
            return self._delete_definition(request.get("def_id", ""))
```

Place it near `record_annotation` / `quick_record_list` etc. — alphabetical or grouped by surface, your choice. Match the existing style.

- [ ] **Step 5: Refactor the HTTP route to delegate.**

In `packages/collect/fulcra_collect/routes/definitions.py:224-338`, replace the body of `delete_definition_route(def_id)` with:

```python
    @app.delete("/api/definitions/{def_id}", dependencies=[Depends(require_token)])
    def delete_definition_route(def_id: str):
        """Soft-delete a Fulcra annotation definition (task #42).

        HTTP shim over Daemon._delete_definition (the business logic
        moved to daemon.py in SP2 task 1 so the menubar can call the
        same code path via UDS). Returns the same shape as before;
        translates {"ok": False, "error": ...} back into HTTPException
        for HTTP callers.
        """
        result = daemon._delete_definition(def_id)
        if result.get("ok"):
            return result
        # Translate UDS error returns back into HTTPException for the
        # HTTP surface — keeps the HTTP API contract identical to pre-SP2.
        err = result.get("error", "delete failed")
        if "not found" in err.lower():
            raise HTTPException(404, err)
        if "not signed in" in err.lower():
            raise HTTPException(401, err)
        raise HTTPException(502, err)
```

Drop the imports inside the function that are no longer needed (e.g., the late `_web` import — that's now in `daemon._delete_definition`).

- [ ] **Step 6: Run the failing test plus the existing HTTP route tests.**

```bash
cd packages/collect && uv run pytest tests/test_daemon_delete_definition.py tests/test_routes_definitions.py -v
```

Expected: all PASS. The HTTP-side tests still pass because the route now delegates, but the externally-observable behaviour is unchanged.

- [ ] **Step 7: Run the full collect suite.**

```bash
cd packages/collect && uv run pytest -q
```

Expected: 363 passed (prior 360 + 3 new), 0 failed.

- [ ] **Step 8: Commit.**

```bash
git add packages/collect/fulcra_collect/daemon.py \
       packages/collect/fulcra_collect/routes/definitions.py \
       packages/collect/tests/test_daemon_delete_definition.py
git commit -m "refactor(collect): extract delete_definition into Daemon method + UDS command (SP2 task 1)

The soft-delete logic lived only in the HTTP route at
routes/definitions.py:225. SP2 Tasks 3 and 4 (menubar Annotations
Preferences tab + popover \"…\" per-row Delete) need to trigger the
same logic from non-HTTP surfaces. Refactor into a Daemon method
that the HTTP route now wraps; expose it via a new UDS command
\`delete_definition\` so DaemonClient.delete_definition (next commit)
can reach it.

Behaviour-preserving on the HTTP side — same response shape on
success (ok: true, cleared_plugins: [...]) and failure (still
returns 401/404/502 with the same messages, just translated from
the new UDS return shape inside the route wrapper).

3 new tests cover the UDS path's happy case, missing-def_id input
validation, and Fulcra-404 surfacing.

Refs SP2 D1, drift audit 2026-05-27."
```

---

## Task 2: Menubar DaemonClient — new delete_definition method

**Files:**
- Modify: `packages/menubar/fulcra_menubar/daemon_client.py`

- [ ] **Step 1: Add the method.**

In `packages/menubar/fulcra_menubar/daemon_client.py`, add the new method after `delete_annotation` (the sibling pattern at line 127). Place alphabetically among the public methods or grouped by surface — match the file's existing convention.

```python
    def delete_definition(self, def_id: str) -> dict:
        """Soft-delete an annotation definition.

        Wraps the daemon's UDS command introduced in SP2 task 1.
        Returns the daemon's reply dict — {"ok": True, "cleared_plugins":
        [...]} on success, {"ok": False, "error": "..."} on any failure
        mode. Caller is responsible for surfacing the error to the user
        (e.g., NSAlert) and not retrying the same UUID.

        Args:
            def_id: the annotation definition UUID.
        """
        return self._send({"cmd": "delete_definition", "def_id": def_id})
```

- [ ] **Step 2: Python syntax check.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/daemon_client.py').read())"
```

Expected: no output.

- [ ] **Step 3: Commit.**

```bash
git add packages/menubar/fulcra_menubar/daemon_client.py
git commit -m "feat(menubar): DaemonClient.delete_definition for SP2 (SP2 task 2)

One-method addition. Sends the new \`delete_definition\` UDS command
introduced in the previous commit; returns the daemon's reply dict
unchanged so the caller (Annotations Preferences tab, popover '…'
menu) can handle the ok/error cases.

Refs SP2 D1, drift audit 2026-05-27."
```

---

## Task 3: New Annotations Preferences tab

**Files:**
- Create: `packages/menubar/fulcra_menubar/preferences/annotations_tab.py`
- Modify: `packages/menubar/fulcra_menubar/preferences/window.py`

**Why:** Per user Q1 + Q4 + the scoping doc, a new Preferences tab parallel to Plugins/Notifications/About that mirrors the web UI's `/?route=settings` page: bulk favorites checkbox toggle + per-row Delete button with NSAlert confirmation.

- [ ] **Step 1: Create the new tab file.**

Create `packages/menubar/fulcra_menubar/preferences/annotations_tab.py`:

```python
"""Preferences → Annotations tab — manage annotation definitions.

Mirrors the web UI's /?route=settings page in scope:

  - Bulk favorites: a checkbox column on each row toggles the def's
    pinned-in-quick-record status. Edits write through the existing
    set_quick_record_favorites UDS command.
  - Per-row soft-delete: a Delete button on each row triggers an
    NSAlert confirmation, then calls delete_definition (SP2 task 2).

Created as part of SP2 (drift audit 2026-05-27). Q1 answer:
\"New Preferences tab + per-row '…'\" — the tab is the bulk view; the
popover quick-record '…' menu (SP2 task 4) is the per-row affordance.
Q4 answer: simple NSAlert confirmation.
"""
from __future__ import annotations

import logging
from typing import Any

from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSLineBreakByTruncatingMiddle,
    NSScrollView,
    NSSwitch,
    NSTextField,
    NSView,
)
from Foundation import NSMakeRect

from .. import theme
from ..daemon_client import DaemonClient

_log = logging.getLogger("fulcra_menubar.preferences.annotations")

_TAB_W = 640
_TAB_H = 480
_ROW_H = 56  # name + type + favorites switch + delete button per row


def _attach(control, handler):
    """Re-implement the per-tab _attach idiom used in sibling tabs.

    Look at preferences/about_tab.py:_attach for the pattern; the
    docstring there explains why we keep a strong ref to the target.
    Match it byte-for-byte; the pattern is repeated across tabs
    rather than centralised in a helper module on purpose (one of
    the file conventions in this codebase — see preferences/plugins_tab.py
    for another instance).
    """
    # Copy the implementation from about_tab.py:_attach verbatim.
    # If a future refactor centralises this, lift it from there.
    ...  # IMPLEMENTER: copy from preferences/about_tab.py


def build_annotations_tab(client: DaemonClient) -> NSView:
    """Build and return the NSView root for the Annotations tab.

    Layout (top-down in AppKit y-coords; tab height = 480):
      y=480-24 .. y=480-44:  Header "Annotations"
      y=480-72 .. y=80:      Scrollable list of definition rows (one per def)
      y=80     .. y=16:      (reserved for footer / status)
    Each row is _ROW_H pt tall:
      [Name (label)] [annotation_type (small caption)]  [pinned NSSwitch] [Delete button]
    """
    colors = theme.colors
    typography = theme.typography

    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _TAB_W, _TAB_H))

    # ------- Header -------
    title = NSTextField.labelWithString_("Annotations")
    title.setFont_(typography.heading())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, _TAB_H - 36, 400, 20))
    view.addSubview_(title)

    subtitle = NSTextField.labelWithString_(
        "Pin tracks to quick-record (★), or soft-delete a track you "
        "no longer want in your pickers. Deleted tracks keep their "
        "already-written events on your Fulcra timeline."
    )
    subtitle.setFont_(typography.small())
    subtitle.setTextColor_(colors.text_secondary())
    subtitle.setFrame_(NSMakeRect(16, _TAB_H - 72, _TAB_W - 32, 28))
    subtitle.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
    view.addSubview_(subtitle)

    # ------- Scrollable list -------
    SCROLL_TOP = _TAB_H - 84
    SCROLL_H = SCROLL_TOP - 16
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(0, 16, _TAB_W, SCROLL_H)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    # We need fresh data each time the tab is built. Pull from the
    # daemon's quick_record_list + get_quick_record_favorites.
    defs_reply = client.quick_record_list()
    favs_reply = client.get_quick_record_favorites()
    defs = (defs_reply or {}).get("definitions", []) or []
    favs = set((favs_reply or {}).get("favorites", []) or [])

    inner_h = max(len(defs) * _ROW_H + 8, SCROLL_H)
    inner = NSView.alloc().initWithFrame_(
        NSMakeRect(0, 0, _TAB_W, inner_h)
    )

    y = inner_h - _ROW_H
    for d in defs:
        def_id = d.get("id", "")
        if not def_id:
            continue
        row = _make_def_row(d, favs, client, y_in_inner=y)
        inner.addSubview_(row)
        y -= _ROW_H

    scroll.setDocumentView_(inner)
    view.addSubview_(scroll)
    return view


def _make_def_row(d: dict[str, Any], favs: set[str],
                  client: DaemonClient, *, y_in_inner: float) -> NSView:
    """Build a single row in the Annotations list."""
    colors = theme.colors
    typography = theme.typography
    def_id = d.get("id", "")
    name = d.get("name", "(unnamed)")
    ann_type = d.get("annotation_type", "")

    row = NSView.alloc().initWithFrame_(
        NSMakeRect(0, y_in_inner, _TAB_W, _ROW_H)
    )

    name_lbl = NSTextField.labelWithString_(name)
    name_lbl.setFont_(typography.body())
    name_lbl.setTextColor_(colors.text())
    name_lbl.setFrame_(NSMakeRect(16, _ROW_H - 28, _TAB_W - 200, 18))
    name_lbl.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
    row.addSubview_(name_lbl)

    type_lbl = NSTextField.labelWithString_(ann_type)
    type_lbl.setFont_(typography.small())
    type_lbl.setTextColor_(colors.text_secondary())
    type_lbl.setFrame_(NSMakeRect(16, _ROW_H - 48, _TAB_W - 200, 16))
    row.addSubview_(type_lbl)

    # Favorites switch ("pinned in quick record"). Toggling sends the
    # full new favorites list (set semantics — daemon takes the list
    # verbatim and stores it).
    pinned = def_id in favs
    fav_switch = NSSwitch.alloc().initWithFrame_(
        NSMakeRect(_TAB_W - 180, _ROW_H - 36, 50, 22)
    )
    fav_switch.setState_(1 if pinned else 0)

    def on_pin_toggle(sender):
        cur = client.get_quick_record_favorites() or {}
        favs_now = set(cur.get("favorites", []) or [])
        if sender.state():
            favs_now.add(def_id)
        else:
            favs_now.discard(def_id)
        client.set_quick_record_favorites(list(favs_now))

    _attach(fav_switch, on_pin_toggle)
    row.addSubview_(fav_switch)

    # Delete button. NSAlert on press, then call delete_definition.
    del_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(_TAB_W - 96, _ROW_H - 36, 80, 22)
    )
    del_btn.setTitle_("Delete…")
    del_btn.setBezelStyle_(NSBezelStyleRounded)

    def on_delete(_sender):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"Delete \"{name}\"?")
        alert.setInformativeText_(
            "This removes the track from your pickers. Events already "
            "written under this track stay on your Fulcra timeline."
        )
        alert.addButtonWithTitle_("Delete")
        alert.addButtonWithTitle_("Cancel")
        response = alert.runModal()
        if response != NSAlertFirstButtonReturn:
            return
        result = client.delete_definition(def_id)
        if not result.get("ok"):
            err_alert = NSAlert.alloc().init()
            err_alert.setMessageText_("Could not delete")
            err_alert.setInformativeText_(
                result.get("error", "Unknown daemon error.")
            )
            err_alert.addButtonWithTitle_("OK")
            err_alert.runModal()
            return
        # On success, the row's visual state is now stale; we rely on
        # the user closing+reopening Preferences to see the refreshed
        # list. A nicer future fix: subscribe to a daemon model so the
        # tab rebuilds. For SP2 we accept the close-and-reopen pattern.
        _log.info("delete_definition succeeded for %s; close+reopen Preferences to refresh.", def_id)

    _attach(del_btn, on_delete)
    row.addSubview_(del_btn)

    return row
```

The `...` placeholder for `_attach` — copy the body from `preferences/about_tab.py`'s `_attach` verbatim. If a centralised helper already exists somewhere (check `preferences/__init__.py` or `theme/`), use that instead.

- [ ] **Step 2: Register the new tab in `window.py`.**

In `packages/menubar/fulcra_menubar/preferences/window.py`, find the tab construction (likely a sequence of `tabView.addTabViewItem_(...)` calls building Plugins / Notifications / About). Add a new tab between Plugins and Notifications:

```python
from .annotations_tab import build_annotations_tab

# ... existing setup ...

annotations_view = build_annotations_tab(client)
annotations_item = NSTabViewItem.alloc().initWithIdentifier_("annotations")
annotations_item.setLabel_("Annotations")
annotations_item.setView_(annotations_view)
tab_view.addTabViewItem_(annotations_item)
```

The exact tab-add idiom may differ from the above — read the file's existing tab additions and match the pattern. Place the new tab between Plugins and Notifications.

- [ ] **Step 3: Python syntax check.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/preferences/annotations_tab.py').read())"
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/preferences/window.py').read())"
```

Expected: no output for either.

- [ ] **Step 4: Commit.**

```bash
git add packages/menubar/fulcra_menubar/preferences/annotations_tab.py \
       packages/menubar/fulcra_menubar/preferences/window.py
git commit -m "feat(menubar): Annotations Preferences tab — bulk favorites + soft-delete (SP2 task 3)

New tab parallel to Plugins / Notifications / About. Mirrors the web
UI's /?route=settings page in scope:

  - Per-row favorites switch (pin to quick-record), writing through
    the existing set_quick_record_favorites UDS path.
  - Per-row Delete button, NSAlert confirmation, then call the new
    DaemonClient.delete_definition (SP2 task 2).

Wires via DaemonClient — no direct HTTP calls from the menubar.
Tab refreshes via close+reopen Preferences for now; future polish
could subscribe the tab to a daemon-side model.

Per user Q1 + Q4 from the menubar drift audit 2026-05-27.

Refs SP2 D1 + D2."
```

---

## Task 4: Popover quick-record "…" per-row menu

**Files:**
- Modify: `packages/menubar/fulcra_menubar/popover/quick_record.py`

**Why:** Per Q1's second half: the popover's quick-record rows need a small "…" affordance so the one-off "I made this by accident" case doesn't require opening Preferences. The "…" opens an NSMenu with a single "Delete this track…" item that triggers the same NSAlert + delete_definition flow.

- [ ] **Step 1: Identify the row builders.**

Two builders exist in `popover/quick_record.py`:
- `_make_definition_row` (around line 574) — moment-kind rows, simpler.
- `_make_duration_row` (around line 685) — duration-kind rows, more complex.

Both need a "…" button added. Place it in the row's far right, after the existing controls. Width: 24pt, height: 22pt. Should fit within the existing 360pt popover width without forcing a layout change in the surrounding bands.

- [ ] **Step 2: Add a shared `_show_delete_alert(def_id, def_name, client, on_done)` helper.**

Near the top of `popover/quick_record.py` (after imports, before any row builder), add:

```python
def _show_delete_alert(def_id: str, def_name: str,
                       client: DaemonClient,
                       on_done) -> None:
    """Show an NSAlert confirming soft-delete, then call delete_definition.

    Shared between _make_definition_row and _make_duration_row's '…'
    menus, and parallel in behaviour to the Annotations Preferences
    tab's Delete button (SP2 task 3). Per user Q4: simple NSAlert
    confirmation, no two-step.

    Args:
        def_id: UUID of the definition to delete.
        def_name: human-readable name, for the alert title.
        client: DaemonClient instance.
        on_done: callable invoked after a successful delete so the
            caller can refresh its list / row state.
    """
    alert = NSAlert.alloc().init()
    alert.setMessageText_(f"Delete \"{def_name}\"?")
    alert.setInformativeText_(
        "This removes the track from your pickers. Events already "
        "written under this track stay on your Fulcra timeline."
    )
    alert.addButtonWithTitle_("Delete")
    alert.addButtonWithTitle_("Cancel")
    response = alert.runModal()
    if response != NSAlertFirstButtonReturn:
        return
    result = client.delete_definition(def_id)
    if not result.get("ok"):
        err = NSAlert.alloc().init()
        err.setMessageText_("Could not delete")
        err.setInformativeText_(result.get("error", "Unknown daemon error."))
        err.addButtonWithTitle_("OK")
        err.runModal()
        return
    on_done()
```

You'll need to add `NSAlert` and `NSAlertFirstButtonReturn` to the file's existing `from AppKit import` block if they aren't already there.

- [ ] **Step 3: Add the "…" button + NSMenu in each row builder.**

Inside `_make_definition_row` (moment rows), AFTER the existing Record button frame (~line 615), add:

```python
    # "…" per-row menu — gateway to row-level actions (currently only
    # Delete; can grow to e.g. "Edit name" in the future). Per Q1 from
    # the SP2 brainstorming pass — gives users the one-off "I made this
    # by accident" path without leaving the popover.
    more_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - 28, row_y, 20, 22)
    )
    more_btn.setTitle_("…")
    more_btn.setBezelStyle_(NSBezelStyleRoundRect)

    def _on_more(sender):
        menu = NSMenu.alloc().init()
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Delete this track…", None, "",
        )

        def _delete_handler(_):
            _show_delete_alert(
                def_id, def_name, client,
                on_done=lambda: row.removeFromSuperview(),
            )
        _attach(item, _delete_handler)
        menu.addItem_(item)
        NSMenu.popUpContextMenu_withEvent_forView_(
            menu, NSApp.currentEvent(), sender,
        )

    _attach(more_btn, _on_more)
    row.addSubview_(more_btn)
```

You'll need to:
- Adjust the existing `Record` button's x-coord to leave room for the "…" — shift Record left by ~28pt or shrink any wider element (the comment field for moments).
- Add `NSMenu`, `NSMenuItem`, `NSApp` to the AppKit imports if not already present.
- Import `_show_delete_alert` (it's in the same file, no import needed).

For `_make_duration_row` (Duration rows), apply the same pattern after the Timer button position. The Timer button is at `x = width - TIMER_W - 12`, so the "…" sits even further left, OR move Timer left by 28pt and put "…" where Timer was. Pick the layout that doesn't break the SP1 L1 fix (the Record↔Timer gap should stay ≥24pt). Practical placement:
- Timer at `x = width - TIMER_W - 12 - 28` (shift left by 28pt to make room for the new "…")
- "…" at `x = width - 28`

Confirm the gap math: Record ends at `16 + 120 + 6 + 64 + 6 + 56 = 268`. Timer at `width - 56 - 12 - 28 = 264`. WAIT — that puts Timer LEFT of Record. Bad.

Re-plan: keep Timer at `x = width - TIMER_W - 12 - 28` only if it doesn't collide. With width=360, `360 - 56 - 12 - 28 = 264`. Record ends at 268. Collision.

Alternative: keep Timer where it is at `width - TIMER_W - 12 = 292`, and put the "…" at the FAR right, OUTSIDE the existing 360pt width by extending Duration rows alone to 388pt? No — that breaks the popover.

Cleaner alternative: shrink the comment field further (COMMENT_W: 120 → 92) to make room for the "…" at the far right while keeping Timer where it is. New Record-end: `16 + 92 + 6 + 64 + 6 + 56 = 240`. Timer at 292. Gap = 52pt (huge — too wasteful). Put the "…" at `x = 268`, width 20, ends at 288. Timer at 292. Gap between "…" and Timer: 4pt — TOO TIGHT.

Best fit: COMMENT_W: 120 → 96 (shrink 24pt). Record ends at `16+96+6+64+6+56 = 244`. "…" at `x = 252`, ends at 272. Timer at 292. Gap "…"-to-Timer = 20pt; gap Record-to-"…" = 8pt. Hmm.

The cleanest layout is to put "…" to the LEFT of Record, near the start of the row, OR to put it on the row's first line (name/star/timer-hint line) instead of the controls line.

**Recommended approach:** put "…" on the FIRST line (the name/star/timer-hint line at `y = name_y`). It's much less crowded up there. Place at `x = width - 24` (far right of the first line); the existing timer-hint at `x = width - 180` doesn't conflict.

For moment rows the first line is the same line as the controls, so this only helps duration rows. For moment rows, accept the "…" eats some width — shrink the comment field (or wherever there's slack) by 28pt to fit.

The implementer should pick a placement that:
1. Doesn't break SP1 L1 (Record↔Timer gap stays ≥24pt on duration rows).
2. Stays inside the 360pt popover width.
3. Is consistently positioned across moment and duration rows.

If unsure, place on the first line (name row) for both kinds.

- [ ] **Step 4: Python syntax check.**

```bash
python3 -c "import ast; ast.parse(open('packages/menubar/fulcra_menubar/popover/quick_record.py').read())"
```

Expected: no output.

- [ ] **Step 5: Commit.**

```bash
git add packages/menubar/fulcra_menubar/popover/quick_record.py
git commit -m "feat(menubar): popover quick-record per-row '…' menu with Delete (SP2 task 4)

Adds a '…' per-row menu button to both moment and duration row
builders in popover/quick_record.py. The menu currently has one
item — 'Delete this track…' — that triggers the same NSAlert +
delete_definition flow as the Annotations Preferences tab (SP2
task 3), via a shared _show_delete_alert helper.

Per user Q1 + Q4: the popover '…' is the per-row affordance for
the one-off 'I made this by accident' case; the Annotations
Preferences tab is for bulk management.

Placement: '…' on each row's first/header line at x = width - 24,
which avoids colliding with the Record/Timer controls and preserves
the SP1 L1 Record↔Timer gap (24pt).

On successful delete, the row removes itself from the popover view;
the daemon's quick-record cache is busted server-side so the next
list call won't resurrect it.

Refs SP2 D1, drift audit 2026-05-27."
```

---

## Task 5: Rebuild menubar + manual verification

- [ ] **Step 1: Reinstall menubar.**

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

Expected: `Installed 1 executable: fulcra-menubar`.

- [ ] **Step 2: Restart daemon AND menubar.**

```bash
launchctl kickstart -k gui/$(id -u)/com.fulcra.collect
sleep 3
pkill -f fulcra-menubar 2>/dev/null
sleep 1
fulcra-menubar 2>&1 &
sleep 3
ps aux | grep -E "fulcra-menubar|com\.fulcra\.collect" | grep -v grep | head -4
```

Expected: both processes present. Capture the new menubar PID.

- [ ] **Step 3: Update running-process memory.**

Update `/Users/Scanning/.claude/projects/-Users-Scanning-Developer-fulcra-tools/memory/project_fulcra_menubar_running.md` with the new PID + the SP2 commits referenced.

- [ ] **Step 4: Full pytest sweep.**

```bash
uv run --all-packages pytest -q packages/ 2>&1 | tail -3
```

Expected: 1447 passed (1444 baseline + 3 new from Task 1), 1 skipped.

- [ ] **Step 5: Orphan/obsolete sweep.**

```bash
git diff 4fb591e..HEAD --stat
git diff 4fb591e..HEAD -- packages/menubar/fulcra_menubar/popover/quick_record.py
```

Specifically check: with SP2 Task 4's "…" button added to rows, does any other comment in `quick_record.py` describe the per-row layout as "Record + Timer only"? Update any stale layout descriptions.

If found, follow-up commit:
```bash
git add packages/menubar/...
git commit -m "chore(menubar): orphan/obsolete sweep after SP2 (SP2 follow-up)

[describe findings]"
```

- [ ] **Step 6: Surface manual walkthrough.**

The user needs to verify (no autonomous test path for AppKit):
1. **Preferences → new "Annotations" tab** between Plugins and Notifications — opens with a list of all annotation definitions. Toggle a favorites switch — pin reflects in the menubar quick-record submenu. Click Delete → NSAlert appears → confirm → row disappears from web UI's quick-record favorites AND from the menubar list.
2. **Popover quick-record** — each row now has a "…" button. Click it → menu opens with "Delete this track…" item. Click → NSAlert → confirm → row vanishes.
3. **No regression** to Quick Record's Record/Timer buttons on Duration rows (SP1 L1's 24pt gap holds).
4. **No regression** to the Plugins tab (SP1 L2 dynamic-height descriptions still work).

---

## Final cross-cutting code review

After all 5 tasks land, dispatch the superpowers:code-reviewer agent over the combined diff `4fb591e..HEAD`. Cover:
- Daemon-side refactor preserved HTTP-route contract byte-for-byte?
- New UDS command's error shape consistent with sibling commands?
- New Annotations tab visually consistent with sibling tabs (spacing, typography, button styles)?
- "…" menu placement doesn't collide with SP1 L1's Record↔Timer gap?
- Any orphan code in the route module after the refactor?

## Acceptance Checklist

- [ ] `cd packages/collect && uv run pytest -q` passes 363+ tests (was 360 + 3 new).
- [ ] Full workspace sweep at 1447+ passed.
- [ ] `node --check` not relevant — no JS changes.
- [ ] `python3 -c "import ast; ast.parse(...)"` clean on all touched Python files.
- [ ] Menubar relaunches cleanly with the new tab visible.
- [ ] HTTP DELETE /api/definitions/{def_id} still works (smoke-test via curl + a throwaway def UUID).
- [ ] No regression in SP1 L1 / L2 / L3 visual states.

## Risks

| Risk | Mitigation |
|---|---|
| HTTP route's response shape silently changes during the refactor (e.g., `cleared_plugins` key disappears or `error` shape drifts). | Existing HTTP-route tests in `test_routes_definitions.py` are the regression net. Task 1 Step 6 explicitly re-runs them. |
| The "…" button collides with SP1 L1's Record↔Timer 24pt gap on Duration rows. | Plan recommends placing "…" on the first (header) row line where there's slack, NOT on the controls line. Manual walkthrough Step 5.3 explicitly checks. |
| New Annotations tab's `_attach` helper drifts from the sibling tabs' implementations. | Plan instructs copying from `about_tab.py` verbatim. Future refactor could centralise. |
| The daemon's `_get_fulcra_token` helper doesn't exist (or is named differently). | Task 1 Step 3 instructs checking + providing equivalent. The HTTP route's `fulcra_token_or_401` is closure-injected; the UDS equivalent reads directly from the keychain. |
