"""fulcra-csv CLI: bootstrap an annotation def, import a CSV, list defs."""

from __future__ import annotations

from datetime import timezone, tzinfo
from pathlib import Path

import click

from .events import ColumnMap
from .fulcra import FulcraClient
from .parser import parse_csv


def _parse_extras(extras: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Parse `--extra COL=KEY` flags into a tuple of (col, key) pairs."""
    pairs: list[tuple[str, str]] = []
    for item in extras:
        if "=" not in item:
            raise click.UsageError(f"--extra requires COL=KEY format, got {item!r}")
        col, key = item.split("=", 1)
        pairs.append((col.strip(), key.strip()))
    return tuple(pairs)


def _resolve_tz(tz_name: str) -> tzinfo:
    if tz_name == "UTC":
        return timezone.utc
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        raise click.UsageError(f"unknown timezone {tz_name!r}: {exc}") from exc


@click.group(help="Import any CSV into Fulcra as DurationAnnotation events.")
def cli() -> None:
    pass


@cli.command(
    "import",
    help="Import a CSV into the given annotation definition.",
)
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--definition-id", required=True,
              help="Target annotation definition UUID")
@click.option("--ts-col", default="timestamp", help="Timestamp column name")
@click.option("--end-col", default=None, help="Optional end-time column")
@click.option("--duration-col", default=None,
              help="Optional duration-in-seconds column (alternative to --end-col)")
@click.option("--title-col", default="title", help="Title column (e.g. track, episode)")
@click.option("--subtitle-col", default=None,
              help="Subtitle column (e.g. artist, show) — joined with title for note")
@click.option("--note-col", default=None,
              help="Override note column (otherwise built from title + subtitle)")
@click.option("--source-id-col", default=None,
              help="Optional column with a stable per-row id (prevents re-ingest dupes)")
@click.option("--tag-col", default=None, help="Per-row tag column (e.g. service name)")
@click.option("--tag", "default_tag", default=None,
              help="Default tag for all rows (overridden by --tag-col if both set)")
@click.option("--extra", "extras", multiple=True,
              help="COL=KEY: lift CSV column COL into external_ids[KEY]. Repeatable.")
@click.option("--tz", "tz_name", default="UTC",
              help="IANA tz for naive timestamps (default UTC)")
@click.option("--source-id-prefix", default="com.fulcradynamics.csv.v1",
              help="Deterministic source-id prefix")
@click.option("--dry-run", is_flag=True, help="Parse and print, don't ingest")
def import_csv(
    csv_path: Path,
    definition_id: str,
    ts_col: str,
    end_col: str | None,
    duration_col: str | None,
    title_col: str,
    subtitle_col: str | None,
    note_col: str | None,
    source_id_col: str | None,
    tag_col: str | None,
    default_tag: str | None,
    extras: tuple[str, ...],
    tz_name: str,
    source_id_prefix: str,
    dry_run: bool,
) -> None:
    colmap = ColumnMap(
        timestamp=ts_col,
        end_time=end_col,
        duration_seconds=duration_col,
        title=title_col,
        subtitle=subtitle_col,
        note=note_col,
        source_id=source_id_col,
        tag=tag_col,
        extras=_parse_extras(extras),
    )
    tz = _resolve_tz(tz_name)
    events = list(parse_csv(
        csv_path,
        column_map=colmap,
        tz=tz,
        source_id_prefix=source_id_prefix,
        default_tag=default_tag,
    ))
    click.echo(f"parsed {len(events)} events from {csv_path}")
    if dry_run:
        for e in events[:5]:
            click.echo(f"  {e.start_time.isoformat()} | {e.tag or '-'} | {e.note}")
        if len(events) > 5:
            click.echo(f"  ... and {len(events) - 5} more")
        return

    client = FulcraClient()
    tag_id_for: dict[str, str] = {}
    unique_tags = {e.tag for e in events if e.tag}
    for tag in unique_tags:
        tag_id_for[tag] = client.ensure_tag(tag)
    result = client.run_import(events, definition_id=definition_id, tag_id_for=tag_id_for)
    click.echo(
        f"total={result.total} skipped_existing={result.skipped_existing} "
        f"posted={result.posted} verified={result.verified}"
    )


@cli.command(help="Create a generic DurationAnnotation definition.")
@click.option("--name", required=True)
@click.option("--description", default="")
@click.option("--tag", "tags", multiple=True, help="Tags to attach (creates if missing)")
def bootstrap(name: str, description: str, tags: tuple[str, ...]) -> None:
    client = FulcraClient()
    tag_ids = [client.ensure_tag(t) for t in tags]
    body = {
        "annotation_type": "duration",
        "name": name,
        "description": description,
        "tags": tag_ids,
        "measurement_spec": {
            "measurement_type": "duration",
            "value_type": "duration",
            "unit": None,
        },
    }
    r = client._client().post(
        "/user/v1alpha1/annotation",
        json=body,
        headers=client._authed_headers(),
    )
    r.raise_for_status()
    click.echo(r.json()["id"])


if __name__ == "__main__":
    cli()
