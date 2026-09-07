"""fulcra-collect plugin: sync Apple Notes into the Fulcra vault.

Runs inside the collect daemon because the Notes store lives in a
TCC-protected group container: the daemon holds Full Disk Access, an
arbitrary shell does not.

Config (config.toml, [plugin_settings.apple-notes]):
    dry_run           = true    # decode and count, write nothing
    limit             = 50      # only process the first N notes
    max_attachment_mb = 25      # skip attachments larger than this
    container         = "..."   # override the group-container path
"""
from __future__ import annotations

from datetime import timedelta
import time
from pathlib import Path

from fulcra_collect.plugin import Permission, Plugin, RunContext, Setting, SetupStep

from . import report
from .notestore import AccessDeniedError, DEFAULT_GROUP_CONTAINER
from .sync import run_reconcile, run_sync

_FULL_DISK_ACCESS = Permission(
    id="full-disk-access",
    explanation=(
        "Reads the Apple Notes SQLite store and attachment files, which live "
        "in a group container macOS guards behind Full Disk Access."
    ),
)


def _container(ctx: RunContext) -> Path:
    override = ctx.config.get("container")
    return Path(override).expanduser() if override else DEFAULT_GROUP_CONTAINER


def run(ctx: RunContext) -> None:
    mode = str(ctx.config.get("mode", "sync")).strip().lower()
    if mode == "reconcile":
        return _run_reconcile(ctx)
    if mode == "writeback":
        return _run_writeback(ctx)
    dry_run = bool(ctx.config.get("dry_run", False))
    limit = ctx.config.get("limit")
    started = time.monotonic()
    try:
        stats = run_sync(
            container=_container(ctx),
            dry_run=dry_run,
            limit=int(limit) if limit else None,
            max_attachment_mb=int(ctx.config.get("max_attachment_mb", 25)),
            log=ctx.log,
            progress=lambda **kw: ctx.progress(**kw),
        )
    except Exception as exc:
        # A crashed run must leave evidence, not just a state-db outcome.
        report.write({"ok": False, "dry_run": dry_run,
                      "error": f"{type(exc).__name__}: {exc}",
                      "elapsed_s": round(time.monotonic() - started, 2)})
        raise
    payload = {"ok": not bool(stats.errors), "dry_run": dry_run,
               "elapsed_s": round(time.monotonic() - started, 2),
               **stats.as_dict()}
    report.write(payload)
    ctx.log.info("apple-notes: %s", payload)
    ctx.progress(stage="done", **{
        k: v for k, v in stats.as_dict().items() if isinstance(v, int)})
    if stats.errors:
        raise RuntimeError("Apple Notes import completed with errors; "
                           "progress is saved. See the local run report.")


def permission_check(ctx: RunContext) -> dict:
    """Verify the Notes store is actually readable from this process."""
    import sqlite3
    import tempfile
    from .notestore import NoteStoreError, snapshot

    store = _container(ctx) / "NoteStore.sqlite"
    try:
        with tempfile.TemporaryDirectory() as td:
            copied = snapshot(store, Path(td))
            with sqlite3.connect(f"file:{copied}?mode=ro", uri=True, timeout=2.0) as conn:
                # SELECT 1 does not read the database and accepts an empty file.
                conn.execute("SELECT Z_PK FROM ZICCLOUDSYNCINGOBJECT LIMIT 1").fetchone()
        return {"granted": True, "hint": None}
    except (AccessDeniedError, PermissionError):
        return {"granted": False, "hint": (
            "Open Notes and let it finish syncing. In System Settings → Privacy & Security → Full Disk Access, "
            "Enable access for the app running Collect, then restart Collect "
            "and choose Verify access.")}
    except (NoteStoreError, OSError, sqlite3.Error):
        return {"granted": False, "hint": (
            "Could not read an Apple Notes database. Open Notes on this Mac "
            "and let it finish syncing, then verify Full Disk Access for Collect.")}


PLUGIN = Plugin(
    id="apple-notes",
    name="Apple Notes",
    kind="scheduled",
    collect_mode="live_polled",
    run=run,
    description=(
        "Syncs your Apple Notes into your Fulcra vault as markdown, with "
        "attachments. One-way (Notes -> vault) and additive: it writes only "
        "under vault/notes/apple/, keeps each note's body in an owner-fenced "
        "section so your own edits survive, and marks deleted notes rather "
        "than deleting them. Needs Full Disk Access."
    ),
    default_interval=timedelta(hours=6),
    requires_network=True,
    required_permissions=(_FULL_DISK_ACCESS,),
    permission_check=permission_check,
    category="journal",
    required_settings=(
        Setting(key="dry_run", label="Preview only", kind="toggle", default=False,
                required=False, help="Read your notes without uploading anything."),
    ),
    setup_steps=(
        SetupStep(kind="intro", title="Bring your Apple Notes into Fulcra",
                  body_md="Collect copies notes and available attachments from this Mac "
                  "to your Fulcra vault. It checks for changes every six hours. "
                  "Your original notes stay in Apple Notes. Open Notes first so notes "
                  "from your other devices have time to download."),
        SetupStep(kind="permission_request", title="Allow Collect to read Notes",
                  body_md="Open **System Settings → Privacy & Security → Full Disk Access** "
                  "and add the app running Collect. For the downloaded app, add "
                  "**Fulcra Collect** from Applications. For a source installation, "
                  "add its Python executable. Restart Collect after granting access, "
                  "then choose **Verify access**. Collect uses this permission to read "
                  "the local Notes database. Normal sync leaves your original notes unchanged."),
        SetupStep(kind="input", title="Choose whether to upload",
                  body_md="Leave Preview only off to copy your notes to your Fulcra "
                  "account. Turn it on to check the import without uploading.",
                  settings_keys=("dry_run",)),
        SetupStep(kind="done", title="Ready to sync Apple Notes",
                  body_md="Choose **Enable & start sync** to upload your notes and attachments "
                  "to your Fulcra vault. In preview mode, choose **Enable & run preview** "
                  "to check the import without uploading. Large libraries "
                  "may need several runs; each run saves its progress. Find your "
                  "copies in **vault/notes/apple/**. Edits outside the imported "
                  "section are preserved; edits inside it can be replaced on sync."),
    ),
)


def _run_reconcile(ctx: RunContext) -> None:
    """Report what changed on each side. Writes nothing, needs no consent."""
    started = time.monotonic()
    try:
        result = run_reconcile(
            container=_container(ctx), log=ctx.log,
            limit=int(ctx.config["limit"]) if ctx.config.get("limit") else None,
            download_all=bool(ctx.config.get("download_all", False)))
    except Exception as exc:
        report.write({"ok": False, "mode": "reconcile",
                      "error": f"{type(exc).__name__}: {exc}",
                      "elapsed_s": round(time.monotonic() - started, 2)})
        raise
    payload = {"ok": True, "mode": "reconcile",
               "elapsed_s": round(time.monotonic() - started, 2), **result}
    report.write(payload, path=report.RECONCILE_PATH)
    ctx.log.info("apple-notes reconcile: %s", result["summary"])


def _run_writeback(ctx: RunContext) -> None:
    """Push vault-side edits back into Apple Notes.

    Dry-run unless explicitly disabled: this is the only path in the plugin
    that modifies the user's Apple Notes, and it cannot be undone in bulk.
    """
    from . import writeback

    started = time.monotonic()
    if ctx.config.get("writeback_enabled") is not True:
        raise RuntimeError("Experimental writeback is disabled. It requires separate explicit opt-in.")
    dry_run = bool(ctx.config.get("dry_run", True))
    result = run_reconcile(container=_container(ctx), log=ctx.log)

    # Probing means talking to Notes, which LAUNCHES Notes.app. That is an
    # unwanted side effect for a routine dry run, so only probe when we
    # actually intend to write, or when explicitly asked.
    if dry_run and not ctx.config.get("probe_automation"):
        available, why = None, "not probed (dry run; set probe_automation to check)"
    else:
        available, why = writeback.probe()
    if available is False and not dry_run:
        report.write({"ok": False, "mode": "writeback",
                      "error": why, "blocked": "automation_consent"},
                     path=report.WRITEBACK_PATH)
        raise RuntimeError(f"apple-notes writeback blocked: {why}")

    changes = [reconcile_change(c) for c in result["changes"]]
    with __import__("tempfile").TemporaryDirectory() as td:
        snap = _snapshot_for(ctx, td)
        notes_by_uuid = {n.uuid: n for n in snap[0]}
        attachments_by_note: dict[str, list] = {}
        for att in snap[1]:
            attachments_by_note.setdefault(att.note_uuid, []).append(att)

    allowed, refused = writeback.plan_writeback(
        changes, notes_by_uuid=notes_by_uuid,
        attachments_by_note=attachments_by_note,
        allow_attachment_loss=bool(ctx.config.get("allow_attachment_loss", False)))

    written, failures = 0, []
    for change, note in allowed:
        vault_body = _vault_body_for(change)
        res = writeback.write_note_body(note.pk, vault_body, dry_run=dry_run)
        if res.ok and not res.skipped:
            written += 1
        elif not res.ok:
            failures.append({"uuid": change.uuid, "error": res.error})

    payload = {"ok": True, "mode": "writeback", "dry_run": dry_run,
               "automation_available": available, "automation_detail": why,
               "candidates": len(allowed), "written": written,
               "refused": [{"uuid": c.uuid, "title": c.title, "reason": r}
                           for c, r in refused][:50],
               "refused_count": len(refused), "failures": failures,
               "elapsed_s": round(time.monotonic() - started, 2)}
    report.write(payload, path=report.WRITEBACK_PATH)
    ctx.log.info("apple-notes writeback: %s", payload)


def reconcile_change(raw: dict):
    from .reconcile import Change, Status
    return Change(uuid=raw["uuid"], status=Status(raw["status"]),
                  path=raw.get("path", ""), title=raw.get("title", ""),
                  detail=raw.get("detail", ""))


def _snapshot_for(ctx: RunContext, td: str):
    from pathlib import Path as _P

    from . import notestore
    snap = notestore.snapshot(_container(ctx) / "NoteStore.sqlite", _P(td))
    with notestore.NoteStore(snap) as store:
        return store.notes(), store.attachments()


def _vault_body_for(change) -> str:
    from . import reconcile as _r
    from . import vaultio as _v
    text = _v.read_text(f"/vault/{change.path}")
    _fields, body = _r.parse_vault_note(text)
    if body is None:
        raise RuntimeError(f"{change.path}: owner fence missing; refusing to "
                           "push a file we cannot delimit")
    return body
