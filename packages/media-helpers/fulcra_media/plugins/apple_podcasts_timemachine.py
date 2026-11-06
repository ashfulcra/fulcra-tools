"""Apple Podcasts (Time Machine recovery) — manual one-shot plugin."""
from __future__ import annotations

from fulcra_collect.plugin import Plugin, RunContext, SetupStep

from ..collect_v1 import media_destinations, media_pending, media_run
from ..fulcra import FulcraClient
from ..importers import apple_podcasts as ap
from ..state import DEFAULT_PATH as STATE_PATH
from ..state import load as _state_load
from ._common import (
    DURATION_SPEC,
    consult_twin_cache,
    record_twins_after_post,
    report_import,
)
from .apple_podcasts import _FULL_DISK_ACCESS_PERMISSION


# Legacy spec alias retained for imports; v1 setup uses media_destinations.
APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC: dict = DURATION_SPEC


@media_run
def _run_apple_podcasts_timemachine(ctx: RunContext) -> None:
    """Walk all Time Machine snapshots and import Apple Podcasts history from each.

    This is a one-shot recovery operation — it imports every played episode
    found across every Time Machine backup.  No watermark is advanced because
    the run is manual and non-incremental; source-id dedup in the ingest layer
    prevents duplicate annotations.

    Raises RuntimeError if no Time Machine snapshots are found (volume not
    mounted).  A SnapshotError on an individual snapshot is logged and skipped
    so a single bad backup does not abort the whole recovery walk.
    """
    media_state = _state_load(STATE_PATH)

    snapshots = ap.find_timemachine_snapshots()
    if not snapshots:
        raise RuntimeError(
            "apple-podcasts-timemachine: no Time Machine snapshots found — "
            "a Time Machine volume must be mounted"
        )

    all_events: list = []
    for snap in snapshots:
        try:
            all_events.extend(ap.parse_db(snap))
        except ap.SnapshotError as exc:
            ctx.log.warning(
                "apple-podcasts-timemachine: skipping snapshot %s — %s", snap, exc
            )

    ctx.progress(stage="parsed", count=len(all_events))

    # Cross-source twin dedup: consult the local high-conf twin cache and apply
    # the configured twin policy (default "keep" → no-op) before importing. This
    # plugin imports inline rather than through the shared glue, so it wires the
    # same twin seam by hand. See _common.consult_twin_cache.
    all_events = consult_twin_cache(ctx, all_events)

    client = FulcraClient(collect_context=ctx)
    result = client.run_import(all_events, media_state, claim=ctx.claim_dedup_keys,
                               unclaim=ctx.unclaim_dedup_keys)
    report_import(ctx, result, "Apple Podcasts")

    # Populate the twin cache with this run's high-conf events (after a
    # successful POST only), so a future low-conf twin can defer to them.
    record_twins_after_post(all_events, posted=result.posted, ctx=ctx)
    # No watermark advance — this is a manual, one-shot recovery run.


PLUGIN = Plugin(
    id="apple-podcasts-timemachine",
    name="Apple Podcasts (Time Machine recovery)",
    kind="manual",
    collect_mode="historical",
    run=_run_apple_podcasts_timemachine,
    record_data_types=media_destinations("listened"),
    record_pending=media_pending,
    description=(
        "One-shot recovery: walks every Time Machine backup on the "
        "mounted backup volume, reads the Apple Podcasts SQLite "
        "database from each snapshot, and imports every played episode "
        "found. Run this once if you've lost historical listens but "
        "have an older Time Machine backup that still has them."
    ),
    default_interval=None,
    requires_network=False,
    category="audio",
    canonical_definition_name="Listened",
    required_permissions=(_FULL_DISK_ACCESS_PERMISSION,),
    required_credentials=(),
    setup_steps=(
        SetupStep(
            kind="intro",
            title="What this plugin does",
            body_md=(
                "If you've lost historical Apple Podcasts listens (e.g. "
                "the Podcasts app reset its database), this recovery "
                "tool walks every Time Machine backup on your mounted "
                "backup drive and pulls played episodes from each "
                "snapshot. It's a one-shot manual run — source-id dedup "
                "handles any overlap with the live database."
            ),
        ),
        SetupStep(
            kind="permission_request",
            title="Mount your Time Machine drive and grant access (if needed)",
            body_md=(
                "Make sure your Time Machine backup drive is mounted "
                "before running. macOS may also require Full Disk "
                "Access for the daemon to read backup snapshots — open "
                "**System Settings -> Privacy & Security -> Full Disk "
                "Access** and add **Fulcra Collect** from Applications. If "
                "you run Collect from source, add the terminal app that "
                "starts the daemon instead."
            ),
        ),
        SetupStep(
            kind="definition_picker",
            title="Where should we write recovered listens?",
            body_md=(
                "We can write to your existing 'Listened' data type or "
                "create a new one."
            ),
            annotation_type="duration",
        ),
        SetupStep(
            kind="done",
            title="You're set",
            body_md=(
                "Time Machine recovery is configured. Click **Run "
                "now** from the dashboard to walk every snapshot — this "
                "can take a few minutes."
            ),
        ),
    ),
)
