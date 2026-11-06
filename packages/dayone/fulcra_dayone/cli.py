"""fulcra-dayone command-line interface."""
from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path

import click

from .client import DayOneFulcraClient
from contextlib import nullcontext

from fulcra_common.v1 import _destination
from .filter import select
from .readers import read


def _parse_date(value: str, *, end_of_day: bool) -> datetime:
    """Parse an ISO date (YYYY-MM-DD) to a UTC datetime — start or end of day."""
    d = datetime.fromisoformat(value).date()
    t = time(23, 59, 59, 999999) if end_of_day else time(0, 0, 0)
    return datetime.combine(d, t, tzinfo=timezone.utc)


@click.group()
def cli() -> None:
    """Import Day One journal entries into Fulcra."""


@cli.command(name="import")
@click.argument("source", required=False, type=click.Path(path_type=Path))
@click.option("--local-db", is_flag=True,
              help="Read Day One's local database instead of an export.")
@click.option("--db-path", type=click.Path(path_type=Path), default=None,
              help="Override the local database path.")
@click.option("--tag", "tags", multiple=True,
              help="Only entries carrying this tag (repeatable).")
@click.option("--journal", "journals", multiple=True,
              help="Only entries in this journal (repeatable).")
@click.option("--since", default=None, help="Only entries on/after this ISO date.")
@click.option("--until", default=None, help="Only entries on/before this ISO date.")
@click.option("--starred", is_flag=True, help="Only starred entries.")
@click.option("--all", "import_all", is_flag=True,
              help="Required to import with no filters.")
@click.option("--destination", default=None, help="Pin an owned compatible Event/UUID destination.")
@click.option("--dry-run", is_flag=True,
              help="Show what would be imported; don't contact Fulcra.")
def import_cmd(
    source: Path | None, local_db: bool, db_path: Path | None,
    tags: tuple[str, ...], journals: tuple[str, ...],
    since: str | None, until: str | None, starred: bool,
    import_all: bool, dry_run: bool, destination: str | None,
) -> None:
    """Import Day One entries from SOURCE (a .zip or folder), or --local-db."""
    any_filter = bool(tags or journals or since or until or starred)
    if not any_filter and not import_all:
        raise click.UsageError(
            "No filters given. Pass --all to import every entry, or use "
            "--tag / --journal / --since / --until / --starred."
        )
    if local_db and source is not None:
        raise click.UsageError("Pass either a SOURCE path or --local-db, not both.")
    if not local_db and source is None:
        raise click.UsageError("Provide a SOURCE path (.zip or folder), or use --local-db.")
    if db_path is not None and not local_db:
        raise click.UsageError("--db-path only applies with --local-db.")

    if destination is not None:
        try:
            _destination(destination, 'Event')
        except ValueError:
            raise click.UsageError('Destination must be Event/UUID; legacy annotation UUIDs require explicit v1 mapping.') from None
    since_date = _parse_date(since, end_of_day=False) if since else None
    until_date = _parse_date(until, end_of_day=True) if until else None
    source = source.resolve() if source is not None else None
    db_path = db_path.resolve() if db_path is not None else None
    settings = {
        'source': str(source) if source is not None else None,
        'local_db': local_db, 'db_path': str(db_path) if db_path is not None else None,
        'tags': sorted(set(tags)), 'journals': sorted(set(journals)),
        'since': since_date.isoformat() if since_date else None,
        'until': until_date.isoformat() if until_date else None,
        'starred': starred, 'all': import_all,
        'v1_destination_ids': {'journal': destination} if destination else {},
    }
    scope = nullcontext(None) if dry_run else DayOneFulcraClient().open(settings)
    with scope as writer:
        if writer is not None:
            writer.resume()
        entries = read(source, local_db=local_db, db_path=db_path)
        selected = select(entries, tags=frozenset(tags), journals=frozenset(journals),
            since=since_date, until=until_date, starred_only=starred)
        if not selected:
            click.echo("No entries matched the filters.")
            return
        if dry_run:
            journals_seen = sorted({e.journal for e in selected})
            dates = sorted(e.creation_date for e in selected)
            n = len(selected)
            click.echo(f"Would import {n} {'entry' if n == 1 else 'entries'}.")
            click.echo(f"  journals: {', '.join(journals_seen)}")
            click.echo(f"  date range: {dates[0].date()} .. {dates[-1].date()}")
            return
        result = writer.run_import(selected)
        click.echo(
            f"Imported {result.posted} entries "
            f"({result.skipped_existing} already present, "
            f"{result.verified} verified)."
        )
