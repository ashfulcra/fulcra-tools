"""fulcra-collect plugin: import Day One entries.

run(ctx) reads its source from ctx.config — either {"local_db": "live_app"}
(read from the running Day One app's SQLite DB) or
{"local_db": "export_file", "path": "<export .zip or folder>"} (one-shot
read from a JSON export).  Boolean True is still accepted for the
live-app mode to preserve backwards compatibility with config.toml files
that pre-date the enum.

A scheduled plugin: the daemon fires it every 6 hours so the live-app
mode picks up new entries automatically.  Export-file users can also
trigger it manually via `fulcra-collect run dayone` (or the Run Now
button) — run() naturally no-ops when there are no new entries.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fulcra_collect.plugin import Permission, Plugin, RunContext, Setting, SetupStep

from .client import DayOneFulcraClient
from .convert import to_event
from .filter import select
from .readers import read
from .readers.local_db import find_database


_FULL_DISK_ACCESS_PERMISSION = Permission(
    id="full-disk-access",
    explanation=(
        "Reads the running Day One app's SQLite database, which macOS guards "
        "behind Full Disk Access."
    ),
)


def _live_app_mode(value: object) -> bool:
    """Return True when the local_db setting selects live-app mode.

    Accepts the enum string "live_app" (the new wizard-driven value) as
    well as the legacy boolean True / string "true" / 1 that pre-date the
    enum, so existing config.toml files keep working.
    """
    return value in (True, "live_app", "true", 1)


def run(ctx: RunContext) -> None:
    local_db = _live_app_mode(ctx.config.get("local_db"))
    path_setting = ctx.config.get("path")
    if not local_db and not path_setting:
        raise RuntimeError(
            "dayone: set either `local_db = \"live_app\"` or "
            "`path = \"<export>\"` in this plugin's settings "
            "(config.toml [plugin_settings.dayone])"
        )
    source = Path(path_setting) if path_setting else None
    db_path = Path(ctx.config["db_path"]) if ctx.config.get("db_path") else None

    entries = read(source, local_db=local_db, db_path=db_path)
    selected = select(entries)  # plan 1a imports all entries; filters are a 1b concern
    ctx.progress(stage="read", count=len(selected))
    if not selected:
        return

    events = [to_event(e) for e in selected]
    client = DayOneFulcraClient()
    definition_id = client.ensure_journal_definition()
    tag_names = sorted({t for e in selected for t in getattr(e, "tags", ())})
    tag_id_for = {name: client.ensure_tag(name) for name in tag_names}
    result = client.run_import(events, definition_id=definition_id,
                               tag_id_for=tag_id_for)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)


def dayone_permission_check(ctx: RunContext) -> dict:
    """Verify we can open the Day One SQLite DB.

    Only meaningful in live-app mode — when the user has chosen
    export-file mode there's no DB to open, so the check short-circuits
    to "granted" so the wizard's Verify Access button doesn't falsely
    flag a non-issue.
    """
    import sqlite3

    if not _live_app_mode(ctx.config.get("local_db")):
        return {"granted": True, "hint": "Export-file mode selected — no DB access required."}
    db_path_setting = ctx.config.get("db_path")
    try:
        db_path = Path(db_path_setting) if db_path_setting else find_database()
    except FileNotFoundError as exc:
        return {"granted": False, "hint": str(exc)}
    if not db_path.exists():
        return {"granted": False, "hint": f"Day One database not found at {db_path}."}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"granted": True, "hint": None}
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "permission" in msg or "unable to open" in msg or "authorization denied" in msg:
            return {
                "granted": False,
                "hint": (
                    "Full Disk Access not granted. Add the terminal running "
                    "fulcra-collect to System Settings -> Privacy & Security "
                    "-> Full Disk Access."
                ),
            }
        return {"granted": False, "hint": f"sqlite error: {exc}"}
    except Exception as exc:
        return {"granted": False, "hint": f"{type(exc).__name__}: {exc}"}


PLUGIN = Plugin(
    id="dayone",
    name="Day One journal",
    kind="scheduled",
    collect_mode="live_polled",
    run=run,
    description=(
        "Imports your Day One journal entries as Journal annotations. Pick "
        "between live-app mode (reads the running Day One database every 6 "
        "hours; needs Full Disk Access) or a one-time export ZIP. New "
        "entries become moment annotations in Fulcra."
    ),
    default_interval=timedelta(hours=6),
    category="journal",
    canonical_definition_name="Journal",
    required_permissions=(_FULL_DISK_ACCESS_PERMISSION,),
    required_settings=(
        Setting(
            key="local_db",
            label="Mode",
            kind="enum",
            enum_values=("live_app", "export_file"),
            enum_labels=(
                "Live app (auto-sync every 6h — needs Full Disk Access)",
                "Export file (one-time import of a Day One JSON ZIP/folder)",
            ),
            default="live_app",
            help=(
                "Live app reads the running Day One app's database every 6 "
                "hours; Export file is a one-shot import of a Day One JSON "
                "export ZIP or folder."
            ),
        ),
        Setting(
            key="path",
            label="Day One export ZIP/folder path",
            kind="path",
            required=False,
            help=(
                "Only used in export_file mode. Point at the .zip Day One "
                "produces from File -> Export, or the unzipped folder."
            ),
        ),
        Setting(
            key="db_path",
            label="Custom Day One DB path (advanced)",
            kind="path",
            required=False,
            help=(
                "Override the auto-discovered Day One SQLite path. Leave "
                "blank unless you keep Day One data outside the default "
                "Group Containers location."
            ),
        ),
    ),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="How Day One sync works",
            body_md=(
                "Day One can sync your journal in two ways: from the running "
                "Day One app, OR from a one-time export ZIP. The live-app "
                "mode runs continuously and picks up new entries; the export "
                "mode is one-shot."
            ),
        ),
        SetupStep(
            kind="input",
            title="Pick your mode",
            body_md=(
                "Pick **live_app** if Day One is installed locally and you "
                "want continuous sync. Pick **export_file** if you have a "
                "one-time export ZIP."
            ),
            settings_keys=("local_db",),
        ),
        SetupStep(
            kind="permission_request",
            title="Grant Full Disk Access",
            body_md=(
                "Live-app mode reads Day One's SQLite database at "
                "`~/Library/Group Containers/*.dayoneapp2/Data/Documents/"
                "DayOne.sqlite`, which macOS guards behind Full Disk Access. "
                "Open **System Settings -> Privacy & Security -> Full Disk "
                "Access**, click **+**, and add the terminal you're running "
                "the daemon from (or the bundled fulcra-collect.app once it "
                "exists).\n\n"
                "Also for live-app mode: don't quit Day One entirely — "
                "entries from your other devices sync locally only while "
                "the app is running."
            ),
            condition={"local_db": ("live_app",)},
        ),
        SetupStep(
            kind="file_upload",
            title="Upload your Day One export",
            body_md=(
                "Point at the .zip Day One produces from File -> Export "
                "(or the unzipped folder)."
            ),
            settings_keys=("path",),
            condition={"local_db": ("export_file",)},
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your Day One entries?",
            body_md=(
                "We can write to your existing 'Journal' annotation or "
                "create a new one."
            ),
            annotation_type="moment",
        ),
        SetupStep(
            kind="done",
            title="Day One is set",
            body_md=(
                "Live-app mode polls every 6 hours; export mode runs only "
                "when you click Run Now."
            ),
        ),
    ),
    permission_check=dayone_permission_check,
)
