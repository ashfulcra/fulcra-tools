"""Apple Podcasts (on-device) — scheduled plugin reading the local SQLite DB."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fulcra_collect.plugin import Permission, Plugin, RunContext, SetupStep

from ..apple_podcasts_health import apple_podcasts_health_check
from ..fulcra import FulcraClient
from ..importers import apple_podcasts as ap
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ..state import save as _state_save
from ._common import DURATION_SPEC, ensure_media_def, newest_event_iso


_FULL_DISK_ACCESS_PERMISSION = Permission(
    id="full-disk-access",
    explanation=(
        "Reads the on-device Apple Podcasts database (~/Library/...), which macOS "
        "guards behind Full Disk Access."
    ),
)


def apple_podcasts_permission_check(ctx) -> dict:
    """Verify Full Disk Access by attempting to open the Podcasts SQLite DB.

    Returns {"granted": bool, "hint": str | None}. The wizard's
    permission_request step calls this so it can show a real
    "verified / not verified" status instead of guessing — Full Disk
    Access never produces an in-app prompt, so this round-trip is the
    only way to know whether the user actually added the binary in
    System Settings.
    """
    import sqlite3
    import glob
    pattern = str(
        Path.home() / "Library/Group Containers/*.podcasts*/Documents/MTLibrary.sqlite"
    )
    candidates = glob.glob(pattern)
    if not candidates:
        return {
            "granted": False,
            "hint": (
                "No Podcasts database found — Apple Podcasts may not be "
                "installed or has never run."
            ),
        }
    db_path = candidates[0]
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


# Identical structure to LASTFM_LISTENED_SPEC and SPOTIFY_EXTENDED_LISTENED_SPEC.
APPLE_PODCASTS_LISTENED_SPEC: dict = DURATION_SPEC


def _run_apple_podcasts(ctx: RunContext) -> None:
    """Read the local Apple Podcasts SQLite DB and import played episodes.

    Reads ctx.config["db_path"] if set, otherwise uses the default DB location
    (~/Library/Group Containers/…/MTLibrary.sqlite).  The whole DB is parsed on
    every run; source-id dedup in the ingest layer handles re-imports.  The
    watermark is advanced after a successful run so progress is visible.

    A SnapshotError (the DB is I/O-stalled, e.g. mid-iCloud-sync) is surfaced
    as a RuntimeError with a clear message so the scheduler can retry later.
    """
    raw_path = ctx.config.get("db_path")
    db_path = Path(raw_path) if raw_path else ap.DEFAULT_DB_PATH

    try:
        events = list(ap.parse_db(db_path))
    except ap.SnapshotError as exc:
        raise RuntimeError(
            f"apple-podcasts: DB snapshot failed — the database is likely "
            f"I/O-stalled (iCloud sync in progress). Try again later. "
            f"Details: {exc}"
        ) from exc

    ctx.progress(stage="parsed", count=len(events))

    # Ensure the "Listened" annotation definition is known before importing.
    # On a fresh install (machine 2) the media state file may have no
    # listened_definition_id because bootstrap was never run on this machine.
    # The shared resolver adopts Machine 1's existing "Listened" definition
    # rather than creating a duplicate.
    media_state = _state_load(STATE_PATH)
    ensure_media_def(ctx, media_state, attr="listened_definition_id",
                     spec=APPLE_PODCASTS_LISTENED_SPEC,
                     canonical_name="Listened",
                     state_save=_state_save)

    client = FulcraClient()
    client.ensure_tag("apple-podcasts", media_state)
    result = client.run_import(events, media_state)
    ctx.progress(stage="imported", posted=result.posted,
                 skipped=result.skipped_existing)
    if result.posted > 0:
        ctx.annotation(
            f"Apple-podcasts: {result.posted} new annotation"
            + ("s" if result.posted != 1 else ""),
            ok=True,
        )

    # Advance even when posted == 0 — see _common.run_scheduled_import for rationale.
    new_wm = newest_event_iso(events)
    if new_wm:
        ctx.state.watermark = new_wm


PLUGIN = Plugin(
    id="apple-podcasts",
    name="Apple Podcasts (on-device)",
    kind="scheduled",
    collect_mode="live_polled",
    run=_run_apple_podcasts,
    description=(
        "Captures podcast episodes you finish on your Mac by reading the "
        "local Apple Podcasts database directly — the app doesn't need to "
        "be open. Runs every 6 hours. Needs Full Disk Access so macOS lets "
        "the daemon read the Podcasts SQLite file."
    ),
    default_interval=timedelta(hours=6),
    requires_network=False,
    category="audio",
    canonical_definition_name="Listened",
    # The wizard's permission_request step currently gates on FDA
    # (nextBlocked = !permissionResult.ok), so FDA is effectively required
    # for onboarding even though the underlying importer would work without
    # it if the daemon process already inherits read access (e.g. its parent
    # has FDA). See task #74 for the planned soft-gate follow-up.
    required_permissions=(_FULL_DISK_ACCESS_PERMISSION,),
    required_credentials=(),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "We read your local Apple Podcasts database every 6 hours — "
                "the Podcasts app doesn't need to be open. iCloud sync stays "
                "fresh only while macOS can run the Podcasts extension in the "
                "background; if you quit the app for days, the DB may fall "
                "behind."
            ),
        ),
        SetupStep(
            kind="permission_request",
            title="Grant Full Disk Access",
            body_md=(
                "Full Disk Access is the fallback path when sandboxing "
                "blocks the direct read of the Podcasts database. Click "
                "Next to test whether access works already. If the test "
                "fails, grant Full Disk Access in **System Settings -> "
                "Privacy & Security -> Full Disk Access** by clicking "
                "**+** and adding the terminal you're running the daemon "
                "from (or the bundled fulcra-collect.app once it exists)."
            ),
        ),
        # Verify the DB is readable before letting the user advance. The
        # wizard's generic test_connection renderer calls
        # /api/plugin/apple-podcasts/health_check, which runs
        # apple_podcasts_health_check (DB open + COUNT of played episodes)
        # and gates Next on ok=True. Without this, a missing-FDA error
        # only surfaces at the first scheduled run hours later.
        SetupStep(
            kind="test_connection",
            title="Verify your Podcasts library",
            body_md=(
                "We'll open your Podcasts database read-only and count "
                "played episodes. No data is sent to Fulcra yet — this "
                "just confirms the file is readable."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write your podcast listens?",
            body_md=(
                "We can write to your existing 'Listened' annotation or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md="Apple Podcasts will sync every 6 hours.",
        ),
    ),
    permission_check=apple_podcasts_permission_check,
    health_check=apple_podcasts_health_check,
)
