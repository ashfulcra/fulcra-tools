"""fulcra-labs command line — the deterministic half of the pipeline.

Commands:
  markers   list / search the canonical marker registry
  check     cross-check two independent extraction passes of one PDF
  ingest    validate extracted observations and (optionally) ingest them
  status    account/profile native evidence and separate legacy attempts

Human output on stdout by default; ``--json`` emits a machine envelope. All
logs go to stderr (see logging_setup) so they never corrupt ``--json``.
"""

from __future__ import annotations

import json
import sys

import click

from . import markers as _markers
from .check import cross_check
from .logging_setup import configure, get_logger
from .store import LabsClient
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
        rows.append(
            {
                "key": m.key,
                "display_name": m.display_name,
                "canonical_unit": m.canonical_unit,
                "loinc": m.loinc,
                "accepted_units": sorted(m.accepted_units),
                "aliases": list(m.aliases),
                "plausible_range": list(m.plausible_range),
                "category": m.category,
            }
        )
    if as_json:
        _emit({"markers": rows, "count": len(rows)}, True)
        return
    for r in rows:
        loinc = r["loinc"] or "—"
        click.echo(
            f"{r['key']:<24} {r['display_name']:<38} {r['canonical_unit']:<14} LOINC {loinc}"
        )
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
    click.echo(f"Agreed: {len(result.observations)}   Disagreements: {len(result.disagreements)}")
    for d in result.disagreements:
        click.echo(f"  ! {d['marker']}: {d['reason']}")
        click.echo(f"      A={d['pass_a']}")
        click.echo(f"      B={d['pass_b']}")
    if out:
        click.echo(f"\nAgreed extraction written to {out}")
    elif result.disagreements:
        click.echo("\nRe-read the PDF for the disagreed rows before ingesting.")


def _render_verdicts(report) -> None:
    click.echo(f"lab={report.lab}  collected_at={report.collected_at}  {report.counts}")
    for v in report.verdicts:
        mark = {"ok": "OK  ", "review": "REVW", "reject": "REJ "}[v.verdict]
        val = "" if v.canonical_value is None else f"{v.canonical_value} {v.canonical_unit}"
        click.echo(
            f"  [{mark}] {v.marker_raw:<28} {v.raw_value:>10} {str(v.raw_unit or ''):<10} -> {val}"
        )
        for reason in v.reasons:
            click.echo(f"          · {reason}")


@cli.command("ingest")
@click.argument("observations", type=click.Path())
@click.option(
    "--source-doc",
    default=None,
    type=click.Path(),
    help="Source PDF archived locally only, after pending recovery.",
)
@click.option("--dry-run", is_flag=True, help="Local validation/hash preview only.")
@click.option("--yes-reviewed", default=None, help="Comma-separated reviewed marker keys.")
@click.option(
    "--marker-target", multiple=True, help="Canonical MARKER=Metric/UUID; repeat pins on recovery."
)
@click.option("--json", "as_json", is_flag=True, help="Emit one JSON envelope.")
def ingest_cmd(observations, source_doc, dry_run, yes_reviewed, marker_target, as_json):
    """Recover original native work, then validate and ingest a new extraction."""
    from .native_v1 import Outcome, normalized_pins, open_labs, preview

    writer = None
    outcome = Outcome(dry_run=dry_run)
    recovery = dict(verified=0, excluded=0)
    report = None

    def capture(value):
        nonlocal writer
        writer = value

    try:
        pins = normalized_pins(marker_target)
        confirmed = {key.strip() for key in (yes_reviewed or "").split(",") if key.strip()}
        if dry_run:
            report = validate_extraction(_load_json(observations))
            outcome, _token = preview(report, confirmed, source_doc, pins)
        else:
            with open_labs(pins, client=build_client(), on_writer=capture) as active:
                report = validate_extraction(_load_json(observations))
                outcome = active.run(report, confirmed, source_doc)
            recovery = writer.recovery
    except Exception as exc:
        if writer is not None:
            outcome, recovery = writer.outcome, writer.recovery
        if as_json:
            _emit(dict(error=str(exc), outcome=outcome.to_dict(), recovery=recovery), True)
        else:
            click.echo(
                f"error: {exc}; verified={outcome.ingested}; excluded={outcome.excluded}", err=True
            )
        raise click.exceptions.Exit(1) from None
    if as_json:
        _emit(
            dict(
                outcome=outcome.to_dict(),
                recovery=recovery,
                verdicts=[v.to_dict() for v in report.verdicts],
            ),
            True,
        )
    else:
        _render_verdicts(report)
        verb, count = (
            ("WOULD ingest", outcome.would_ingest) if dry_run else ("ingested", outcome.ingested)
        )
        click.echo(
            f"\n{verb} {count}/{outcome.total}  review-held={outcome.review_held}  "
            f"rejected={outcome.rejected}  in-run-dupes={outcome.skipped_duplicate}"
        )
        click.echo(f"excluded={outcome.excluded}; marker attribution={outcome.marker_attribution}")


@cli.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
@click.option("--counts/--no-counts", default=True, help="Include last invocation verified counts.")
def status_cmd(as_json, counts):
    """Account/profile native evidence, separate from retained legacy attempts."""
    from .native_v1 import STATUS, legacy_evidence, validated_status

    try:
        from fulcra_collect.standalone_v1 import standalone_runtime

        client = build_client()
        transport = client._transport
        # Status is read-only with respect to pending and native status state.
        with standalone_runtime(
            "cli-labs",
            {"status_read": True},
            client=client,
            transport=transport,
            identity_transport=transport,
        ) as (ctx, session):
            state = validated_status(ctx.kv_get(STATUS + ":" + session.account_id, None))
            legacy = legacy_evidence()
            native = (
                dict(state)
                if state is not None
                else {
                    "state": "no verified invocation evidence",
                    "verified": None,
                    "excluded": None,
                }
            )
            if not counts:
                native.pop("verified", None)
                native.pop("excluded", None)
            payload = dict(
                account_id=session.account_id,
                native=native,
                legacy=dict(
                    last_attempt_evidence=legacy.get("last_ingest"),
                    numeric_annotation_tracks=len(legacy["markers"]),
                ),
                registry_markers=len(_markers.all_markers()),
            )
    except Exception as exc:
        payload = dict(
            error=str(exc), native={"state": "UNKNOWN", "verified": None, "excluded": None}
        )
        if as_json:
            _emit(payload, True)
        else:
            click.echo(str(payload), err=True)
        raise click.exceptions.Exit(1) from None
    if as_json:
        _emit(payload, True)
    else:
        click.echo(str(payload))


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
