"""Shared CLI plumbing — the agent-friendly JSON envelope + emit_result.

Every `fulcra-media import <X>` command builds an ImportEnvelope and passes
it to `emit_result`. When --json mode is set, emit_result writes exactly one
line of JSON to stdout (nothing else) and exits non-zero on failure. In
human mode, errors go to stderr.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

import click

from .fulcra import ImportResult


@dataclass
class ImportEnvelope:
    """Structured per-import result, JSON-serializable.

    Sent verbatim to stdout when --json is set, so the schema is part of the
    public contract. Add fields only at the end; never reorder or rename.
    """
    importer: str
    ok: bool
    total: int = 0
    skipped_existing: int = 0
    posted: int = 0
    verified: int = 0
    since_watermark: str | None = None
    new_watermark: str | None = None
    # Set on --check-only runs: how many events would post if we did run.
    would_post: int | None = None
    errors: list[dict[str, str]] = field(default_factory=list)


def import_result_to_dict(
    importer: str,
    result: ImportResult,
    *,
    since_watermark: str | None,
    new_watermark: str | None,
    would_post: int | None = None,
) -> ImportEnvelope:
    """Adapter — turn a FulcraClient ImportResult into an ImportEnvelope."""
    return ImportEnvelope(
        importer=importer,
        ok=True,
        total=result.total,
        skipped_existing=result.skipped_existing,
        posted=result.posted,
        verified=result.verified,
        since_watermark=since_watermark,
        new_watermark=new_watermark,
        would_post=would_post,
    )


def run_and_emit(
    importer_name: str,
    events: list,
    state: Any,
    *,
    tag_name: str | None,
    check_only: bool,
    json_mode: bool,
    since_watermark: str | None = None,
    new_watermark: str | None = None,
) -> None:
    """Standard ingest pipeline + envelope emission. Used by every importer.

    tag_name: which tag to ensure for the events. None to skip (e.g. when the
        events are tagged per-row via fulcra-csv).
    """
    from .state import save as save_state
    from .fulcra import FulcraClient
    from .cli import STATE_PATH  # local import to avoid cycle at module top

    client = FulcraClient()
    if tag_name and not check_only:
        client.ensure_tag(tag_name, state)
    save_state(state, STATE_PATH)
    result = client.run_import(events, state, check_only=check_only)
    save_state(state, STATE_PATH)

    # Populate the cross-batch twin cache with this run's high-confidence
    # events. Skip on check-only since nothing was actually posted.
    if not check_only and result.posted > 0:
        try:
            from . import twin_cache
            twin_cache.record_imported_events(events)
        except Exception:
            # Cache failures must not break the import — log to stderr.
            import click as _click
            _click.echo("warning: twin cache write failed", err=True)

    envelope = import_result_to_dict(
        importer_name, result,
        since_watermark=since_watermark,
        new_watermark=new_watermark,
        would_post=result.posted if check_only else None,
    )
    emit_result(envelope, json_mode=json_mode)


def emit_result(envelope: ImportEnvelope, *, json_mode: bool) -> None:
    """Print the envelope (JSON or human) and exit non-zero on failure.

    Exit code: 0 when ok=True, 2 when ok=False. (Using 2 to distinguish
    "import failed" from click's own exit code 1 for usage errors.)
    """
    payload = asdict(envelope)
    if json_mode:
        click.echo(json.dumps(payload), nl=True)
    else:
        fields = []
        for k, v in payload.items():
            if k in ("errors",):
                continue
            if v is None or v == [] or v == {}:
                continue
            fields.append(f"{k}={v}")
        click.echo(" ".join(fields))
        for err in envelope.errors:
            click.echo(
                f"  error in {err.get('stage', '?')}: {err.get('message', '?')}",
                err=True,
            )
    if not envelope.ok:
        sys.exit(2)
