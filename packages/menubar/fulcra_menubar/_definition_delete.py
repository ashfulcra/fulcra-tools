"""Shared NSAlert + delete_definition helper used by:

  - preferences/annotations_tab.py — per-row Delete button on the
    Annotations Preferences tab (SP2 task 3).
  - popover/quick_record.py — '…' per-row menu's "Delete this track…"
    item (SP2 task 4).

Both surfaces show the same confirmation flow; this module is the
single source of truth so a copy edit on the alert text or the
error-handling shape only happens once.

Why a module instead of inlined per-call-site copies: when SP2 task 4
landed, the alert body was duplicated byte-for-byte with task 3's
version (different files, same surface). The code-quality reviewer
flagged the duplication as a maintenance hazard. This module is the
fix.

Both call sites pass an ``on_done`` callback that captures the
post-success behaviour specific to their surface — the Preferences tab
removes the row and reflows siblings; the popover triggers a full
quick-record rebuild. The helper is intentionally agnostic about that
shape.
"""
from __future__ import annotations

import logging
from typing import Callable

from AppKit import NSAlert  # type: ignore[import-not-found]

from .daemon_client import DaemonClient

# AppKit constant — PyObjC's re-export through ``from AppKit import …``
# is inconsistent across binding versions (it ships sometimes as a
# module-level name, sometimes only via the underlying framework
# Objective-C enum), so we define it locally. NSAlertFirstButtonReturn
# = 1000 per the AppKit headers; this is the response code the modal
# returns when the *first* button (here: "Delete") is clicked.
_NSAlertFirstButtonReturn = 1000

_log = logging.getLogger("fulcra_menubar.delete_definition")

# Shared soft-delete copy. Kept as module constants so the menubar alert
# below and the web Settings confirm (settings.js) state the same thing —
# soft-delete removes the track from every picker, keeps already-written
# events on the timeline, and is NOT reliably reversible. The web copy is
# replicated by hand in settings.js (no shared JS module); keep the two in
# sync if either changes.
DELETE_TRACK_BODY = (
    "This removes it from your pickers everywhere — the menubar and the "
    "web app. Events already written under this track stay on your Fulcra "
    "timeline, but the track itself may not be recoverable."
)


def delete_track_title(def_name: str) -> str:
    return f'Delete the entire "{def_name}" track?'


def show_delete_alert(def_id: str, def_name: str,
                      client: DaemonClient,
                      on_done: Callable[[], None]) -> None:
    """Show an NSAlert confirming soft-delete, then call delete_definition.

    Shared between the Annotations Preferences tab and the popover
    quick-record '…' menu. Per user Q4 from the SP2 brainstorm: simple
    NSAlert confirmation, no two-step undo. The soft-delete is
    reversible server-side via the web Settings page (writes a
    tombstone, not a hard delete) — the alert copy makes that explicit.

    Flow:
      1. Modal confirmation alert. Cancel returns silently.
      2. Call ``client.delete_definition(def_id)``. UDS exceptions are
         caught and surfaced as a daemon-error result, mirroring the
         ``{"ok": False, "error": ...}`` shape the daemon itself uses.
      3. On daemon-side failure, a second NSAlert surfaces the error
         text. ``on_done`` is NOT called.
      4. On success, ``on_done`` runs. The callback owns whatever
         post-delete UI work the caller needs — row removal + reflow
         (Preferences tab), or popover rebuild (quick-record).

    Args:
        def_id: UUID of the definition to delete.
        def_name: human-readable name, for the alert title.
        client: DaemonClient instance — the menubar's UDS bridge.
        on_done: callable invoked exactly once after a successful
            delete so the caller can refresh its list / row state.
    """
    alert = NSAlert.alloc().init()
    alert.setMessageText_(delete_track_title(def_name))
    alert.setInformativeText_(DELETE_TRACK_BODY)
    alert.addButtonWithTitle_("Delete track")
    alert.addButtonWithTitle_("Cancel")
    response = alert.runModal()
    if response != _NSAlertFirstButtonReturn:
        return
    try:
        result = client.delete_definition(def_id)
    except Exception as exc:  # pragma: no cover — UDS transport rare path
        _log.warning("delete_definition raised (%s): %s", def_id, exc)
        result = {"ok": False, "error": str(exc)}
    if not result.get("ok"):
        err = NSAlert.alloc().init()
        err.setMessageText_("Could not delete")
        err.setInformativeText_(result.get("error", "Unknown daemon error."))
        err.addButtonWithTitle_("OK")
        err.runModal()
        return
    _log.info(
        "delete_definition succeeded for %s (cleared_plugins=%s)",
        def_id,
        result.get("cleared_plugins"),
    )
    on_done()
