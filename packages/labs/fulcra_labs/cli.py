"""fulcra-labs command line — the deterministic half of the pipeline.

Commands:
  markers   list / search the canonical marker registry
  check     cross-check two independent extraction passes of one PDF
  ingest    validate extracted observations and (optionally) ingest them
  status    markers known / tracks created / per-track counts / last ingest

Human output on stdout by default; ``--json`` emits a machine envelope. All
logs go to stderr (see logging_setup) so they never corrupt ``--json``.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import click

from . import markers as _markers
from .check import cross_check
from .logging_setup import configure, get_logger
from .store import (
    LabsClient,
    ingest_extraction,
    load_state,
    state_path,
)
from .validate import validate_extraction

log = get_logger(__name__)


def build_client() -> LabsClient:
    """Factory for the live client. Patched in tests."""
    return LabsClient()


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _emit(obj: dict, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(obj, sort_keys=True))


@click.group()
@click.option("--log-level", default=None, help="debug|info|warn|error (or $FULCRA_LABS_LOG)")
def cli(log_level: str | None) -> None:
    """Per-marker lab tracks from PDF extractions — verify before ingest."""
    configure(log_level)


@cli.command("markers")
@click.option("--search", default=None, help="Filter by key / name / alias substring.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def markers_cmd(search: str | None, as_json: bool) -> None:
    """List the canonical marker registry so the agent can resolve aliases."""
    rows = []
    needle = (search or "").strip().lower()
    for m in _markers.all_markers():
        hay = " ".join([m.key, m.display_name, *m.aliases]).lower()
        if needle and needle not in hay:
            continue
        rows.append({
            "key": m.key,
            "display_name": m.display_name,
            "canonical_unit": m.canonical_unit,
            "loinc": m.loinc,
            "accepted_units": sorted(m.accepted_units),
            "aliases": list(m.aliases),
            "plausible_range": list(m.plausible_range),
            "category": m.category,
        })
    if as_json:
        _emit({"markers": rows, "count": len(rows)}, True)
        return
    for r in rows:
        loinc = r["loinc"] or "—"
        click.echo(f"{r['key']:<24} {r['display_name']:<38} "
                   f"{r['canonical_unit']:<14} LOINC {loinc}")
    click.echo(f"\n{len(rows)} marker(s).")


@cli.command("check")
@click.argument("pass_a", type=click.Path(exists=True))
@click.argument("pass_b", type=click.Path(exists=True))
@click.option("--out", default=None, help="Write the agreed extraction JSON here.")
@click.option("--json", "as_json", is_flag=True, help="Emit the full result as JSON.")
def check_cmd(pass_a: str, pass_b: str, out: str | None, as_json: bool) -> None:
    """Cross-check two independent extraction passes of the same PDF."""
    result = cross_check(_load_json(pass_a), _load_json(pass_b))
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result.agreed_extraction(), fh, indent=2, sort_keys=True)
    if as_json:
        _emit(result.to_dict(), True)
        return
    click.echo(f"Agreed: {len(result.observations)}   "
               f"Disagreements: {len(result.disagreements)}")
    for d in result.disagreements:
        click.echo(f"  ! {d['marker']}: {d['reason']}")
        click.echo(f"      A={d['pass_a']}")
        click.echo(f"      B={d['pass_b']}")
    if out:
        click.echo(f"\nAgreed extraction written to {out}")
    elif result.disagreements:
        click.echo("\nRe-read the PDF for the disagreed rows before ingesting.")


def _render_verdicts(report) -> None:
    click.echo(f"lab={report.lab}  collected_at={report.collected_at}  "
               f"{report.counts}")
    for v in report.verdicts:
        mark = {"ok": "OK  ", "review": "REVW", "reject": "REJ "}[v.verdict]
        val = "" if v.canonical_value is None else f"{v.canonical_value} {v.canonical_unit}"
        click.echo(f"  [{mark}] {v.marker_raw:<28} {v.raw_value:>10} "
                   f"{str(v.raw_unit or ''):<10} -> {val}")
        for reason in v.reasons:
            click.echo(f"          · {reason}")


@cli.command("ingest")
@click.argument("observations", type=click.Path(exists=True))
@click.option("--source-doc", default=None, type=click.Path(exists=True),
              help="Path to the source PDF (archived locally, hashed into records).")
@click.option("--dry-run", is_flag=True, help="Validate and show verdicts; write nothing.")
@click.option("--yes-reviewed", default=None,
              help="Comma-separated marker keys to ingest despite a review verdict.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON envelope.")
def ingest_cmd(observations: str, source_doc: str | None, dry_run: bool,
               yes_reviewed: str | None, as_json: bool) -> None:
    """Validate extracted observations and ingest the ``ok`` rows."""
    extraction = _load_json(observations)
    report = validate_extraction(extraction)
    confirmed = {k.strip() for k in (yes_reviewed or "").split(",") if k.strip()}

    client = build_client()
    outcome, report = ingest_extraction(
        client, extraction, state=load_state(), source_doc=source_doc,
        dry_run=dry_run, confirmed_keys=confirmed, report=report,
    )
    if as_json:
        _emit({"outcome": outcome.to_dict(),
               "verdicts": [v.to_dict() for v in report.verdicts]}, True)
        return
    _render_verdicts(report)
    verb = "WOULD ingest" if dry_run else "ingested"
    click.echo(
        f"\n{verb} {outcome.ingested}/{outcome.total}  "
        f"review-held={outcome.review_held}  rejected={outcome.rejected}  "
        f"in-run-dupes={outcome.skipped_duplicate}"
    )
    if outcome.tracks_created:
        click.echo(f"tracks created: {outcome.tracks_created}")
    if outcome.tracks_adopted:
        click.echo(f"tracks adopted: {outcome.tracks_adopted}")
    if dry_run and outcome.review_held:
        click.echo("Re-run without --dry-run (and with --yes-reviewed <keys> to "
                   "override specific review rows) once you've checked the queue.")


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--counts/--no-counts", default=True,
              help="Query per-track observation counts (live reads).")
def status_cmd(as_json: bool, counts: bool) -> None:
    """Markers known / tracks created / observation counts / last ingest."""
    state = load_state()
    tracks = []
    start = datetime(1990, 1, 1, tzinfo=timezone.utc)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    client = build_client() if counts else None
    for key, entry in sorted(state.markers.items()):
        count = None
        if client is not None:
            try:
                count = len(client.marker_series(entry.def_id, start, end))
            except Exception as exc:  # noqa: BLE001 — status must not hard-fail
                log.warning("count read failed for %s: %s", key, exc)
        tracks.append({"key": key, "def_id": entry.def_id,
                       "canonical_unit": entry.canonical_unit, "count": count})
    payload = {
        "registry_markers": len(_markers.all_markers()),
        "tracks_created": len(state.markers),
        "last_ingest": state.last_ingest,
        "state_path": str(state_path()),
        "tracks": tracks,
    }
    if as_json:
        _emit(payload, True)
        return
    click.echo(f"registry markers: {payload['registry_markers']}")
    click.echo(f"tracks created:   {payload['tracks_created']}")
    click.echo(f"last ingest:      {payload['last_ingest']}")
    for t in tracks:
        c = "?" if t["count"] is None else t["count"]
        click.echo(f"  {t['key']:<24} {t['canonical_unit']:<12} count={c}")


def main() -> int:
    try:
        cli(standalone_mode=False)
        return 0
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 — surface crashes visibly (CLAUDE.md)
        click.echo(f"error: {exc}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
