"""Collect plugin contracts for selected local folders and transcript exports."""
from __future__ import annotations

import re
import hmac
from datetime import timedelta
from pathlib import Path
from functools import partial
from copy import deepcopy

from fulcra_collect.plugin import HealthResult, Plugin, RunContext, Setting, SetupStep
from fulcra_collect import config as collect_config, credentials as collect_credentials
from fulcra_collect.config_leases import task_mutation_scope

from .scan import ScanResult, scan_tree
from .sync import SyncResult, sync_files
from .transcripts import render_transcript
from .vault import VaultWriter


LOCAL_FOLDER_EXTENSIONS = {
    ".md", ".txt", ".html", ".htm", ".csv", ".json", ".yaml", ".yml",
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".docx", ".xlsx", ".pptx",
}
TRANSCRIPT_EXTENSIONS = {".md", ".txt", ".vtt", ".srt", ".json"}


def collection_slug(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("collection name is required")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    safe = re.sub(r"-+", "-", safe).strip(".-_")[:80]
    if not safe or safe in {".", ".."}:
        raise ValueError("collection name must contain letters or numbers")
    return safe


def _source(ctx) -> Path:
    value = ctx.config.get("source_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("choose a source folder before running this plugin")
    return Path(value).expanduser()


def _preview(scan: ScanResult) -> list[dict]:
    return [
        {
            "title": item.relative_path.as_posix(),
            "detail": f"{item.size:,} bytes",
        }
        for item in scan.files[:20]
    ]


def _health(ctx, *, extensions: set[str], noun: str) -> HealthResult:
    try:
        collection_slug(ctx.config.get("collection_name"))
        scan = scan_tree(_source(ctx), allowed_extensions=extensions)
    except (OSError, ValueError) as exc:
        return HealthResult(ok=False, summary=str(exc), preview=[])
    skipped = sum(scan.skipped_by_reason.values())
    suffix = f"; {skipped} safely skipped" if skipped else ""
    return HealthResult(
        ok=True,
        summary=f"{len(scan.files)} eligible {noun}{suffix}",
        preview=_preview(scan),
    )


def local_folders_health(ctx: RunContext) -> HealthResult:
    return _health(ctx, extensions=LOCAL_FOLDER_EXTENSIONS, noun="files")


def meeting_transcripts_health(ctx: RunContext) -> HealthResult:
    return _health(ctx, extensions=TRANSCRIPT_EXTENSIONS, noun="transcripts")


def _still_allowed(plugin_id, settings, epoch, token=None, *, fresh=False):
    current = collect_config.load()
    now = current.plugin_settings.get(plugin_id, {})
    current_token = collect_credentials.get_user_secret_fresh("bearer-token") if token and fresh else token
    if ((token is not None and (not current_token or not hmac.compare_digest(token, current_token)))
            or current.account_transition or plugin_id not in current.enabled
            or current.plugin_epochs.get(plugin_id, "") != epoch
            or any(now.get(key, False if key == "dry_run" else None)
                   != settings.get(key, False if key == "dry_run" else None)
                   for key in ("source_path", "collection_name", "dry_run"))):
        raise RuntimeError("Collect configuration changed; run the selected source again")


def _run(
    ctx: RunContext,
    *,
    extensions: set[str],
    destination: str,
    writer,
    render_text=None,
    force_markdown: bool = False,
) -> SyncResult:
    preview = ctx.config.get("dry_run", False)
    if type(preview) is not bool:
        raise ValueError("Preview only must be on or off")
    token = None if preview else ctx.collect_token()
    if not preview and not token:
        raise ValueError("Reconnect to Fulcra in Collect before uploading.")
    allowed = partial(_still_allowed, ctx.plugin_id, deepcopy(ctx.config), ctx.config_epoch, token)
    allowed()
    upload_allowed = partial(allowed, fresh=True)
    collection = collection_slug(ctx.config.get("collection_name"))
    scan = scan_tree(_source(ctx), allowed_extensions=extensions, still_allowed=allowed)
    ctx.progress(
        stage="scanned",
        eligible=len(scan.files),
        skipped=sum(scan.skipped_by_reason.values()),
        bytes=scan.total_bytes,
    )
    owns_writer = writer is None and not preview
    if owns_writer:
        writer = VaultWriter(token)
    try:
        with task_mutation_scope():
            upload_allowed()
            namespace = "preview" if preview else writer.namespace
        result = sync_files(scan.files, writer=writer,
            state_get=ctx.kv_get, state_set=ctx.kv_set,
            destination_root=f"{destination}/{collection}", render_text=render_text,
            force_markdown=force_markdown, dry_run=preview, namespace=namespace,
            still_allowed=upload_allowed, mutation_scope=task_mutation_scope)
    finally:
        if owns_writer:
            writer.close()
    ctx.log.info(
        "%s: eligible=%d uploaded=%d unchanged=%d previewed=%d skipped=%d",
        ctx.plugin_id,
        len(scan.files),
        result.uploaded,
        result.unchanged,
        result.previewed,
        sum(scan.skipped_by_reason.values()),
    )
    ctx.progress(
        stage="done",
        uploaded=result.uploaded,
        unchanged=result.unchanged,
        previewed=result.previewed,
    )
    return result


def run_local_folders(ctx: RunContext, *, writer=None) -> SyncResult:
    return _run(
        ctx,
        extensions=LOCAL_FOLDER_EXTENSIONS,
        destination="/vault/imports/local-folders",
        writer=writer,
    )


def run_meeting_transcripts(ctx: RunContext, *, writer=None) -> SyncResult:
    def render(item):
        try:
            return render_transcript(item.path, relative_path=item.relative_path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(
                f"could not read transcript {item.relative_path.as_posix()}: "
                f"{type(exc).__name__}"
            ) from None

    return _run(
        ctx,
        extensions=TRANSCRIPT_EXTENSIONS,
        destination="/vault/meetings/imported",
        writer=writer,
        render_text=render,
        force_markdown=True,
    )


_COMMON_SETTINGS = (
    Setting(
        key="source_path",
        label="Folder on this Mac",
        kind="path",
        help="Stored only in Collect's private configuration on this Mac.",
    ),
    Setting(
        key="collection_name",
        label="Collection name",
        kind="text",
        help="A short label used for the destination folder in Fulcra.",
    ),
    Setting(
        key="dry_run",
        label="Preview only",
        kind="toggle",
        required=False,
        default=False,
        help="Scan and count eligible files without uploading them.",
    ),
)


LOCAL_FOLDERS_PLUGIN = Plugin(
    id="local-folders",
    name="Local folders and Obsidian",
    kind="scheduled",
    collect_mode="live_polled",
    run=run_local_folders,
    description=(
        "Copies supported files from one folder you choose on this Mac into "
        "Fulcra Files. Works with ordinary document folders and Obsidian vaults."
    ),
    default_interval=timedelta(hours=6),
    requires_network=True,
    required_settings=_COMMON_SETTINGS,
    setup_steps=(
        SetupStep(
            kind="intro",
            title="Choose one folder to copy into Fulcra",
            body_md=(
                "Collect reads supported files from a folder you choose. It never "
                "changes the originals and never deletes a Fulcra copy. Hidden files, "
                "links, credential files, generated folders, and oversized files are skipped."
            ),
        ),
        SetupStep(
            kind="folder_picker",
            title="Choose the folder",
            body_md="Pick an ordinary document folder or an Obsidian vault on this Mac.",
            settings_keys=("source_path",),
        ),
        SetupStep(
            kind="input",
            title="Name this collection",
            body_md=(
                "This becomes the folder name under "
                "`vault/imports/local-folders/`. Turn on Preview only to test without uploading."
            ),
            settings_keys=("collection_name", "dry_run"),
        ),
        SetupStep(
            kind="test_connection",
            title="Review the folder",
            body_md="Collect shows eligible files before you enable the sync.",
        ),
        SetupStep(
            kind="done",
            title="Ready to copy this folder",
            body_md=(
                "Choose **Enable & start sync** to copy eligible files now. Collect "
                "checks the selected folder every six hours and uploads only new or changed files."
            ),
        ),
    ),
    health_check=local_folders_health,
)


MEETING_TRANSCRIPTS_PLUGIN = Plugin(
    id="meeting-transcripts",
    name="Meeting transcript exports",
    kind="scheduled",
    collect_mode="live_polled",
    run=run_meeting_transcripts,
    description=(
        "Watches a folder you choose for Markdown, text, VTT, SRT, and supported "
        "JSON transcript exports, then copies normalized Markdown into Fulcra Files."
    ),
    default_interval=timedelta(minutes=15),
    requires_network=True,
    required_settings=_COMMON_SETTINGS,
    setup_steps=(
        SetupStep(
            kind="intro",
            title="Collect transcript exports without another sign-in",
            body_md=(
                "Point Collect at a folder where your meeting app saves transcript "
                "exports. Collect reads the exports and leaves them unchanged."
            ),
        ),
        SetupStep(
            kind="folder_picker",
            title="Choose the transcript folder",
            body_md="Pick the folder that receives your transcript exports.",
            settings_keys=("source_path",),
        ),
        SetupStep(
            kind="input",
            title="Name these meetings",
            body_md=(
                "This becomes the folder name under `vault/meetings/imported/`. "
                "Turn on Preview only to test without uploading."
            ),
            settings_keys=("collection_name", "dry_run"),
        ),
        SetupStep(
            kind="test_connection",
            title="Review the transcripts",
            body_md="Collect shows the eligible transcript exports before you enable it.",
        ),
        SetupStep(
            kind="done",
            title="Ready to collect transcripts",
            body_md=(
                "Choose **Enable & start sync** to import them now. Collect checks for "
                "new or changed exports every fifteen minutes."
            ),
        ),
    ),
    health_check=meeting_transcripts_health,
)
