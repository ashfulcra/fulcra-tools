"""Apple TV app health check for the fulcra-collect daemon.

Called by the daemon's /api/plugin/apple-tv/health_check route to verify
the TV app's UTS cache is present and readable, and to report how fresh
it is and how many watch events the next scheduled run would import.

Mirrors the shape of apple_podcasts_health.py — same return contract
(HealthResult), same defensive try/except taxonomy. The wizard's generic
test_connection renderer surfaces `summary` in its success banner and
`preview` as a small bullet list so the user can sanity-check they're
seeing their own watch history before clicking Next.

The scan below IS the importer's scan (scan_cache), so the numbers the
wizard shows match exactly what the next scheduled run will parse.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fulcra_collect.plugin import HealthResult

from .importers import apple_tv


def _resolve_cache_dir(ctx) -> Path:
    """Honor an explicit ``cache_dir`` config override (tests / advanced
    users pointing at a copied cache); fall back to the real container."""
    raw = ctx.config.get("cache_dir") if hasattr(ctx, "config") else None
    return Path(raw) if raw else apple_tv.DEFAULT_CACHE_DIR


def _age_str(newest: datetime | None) -> str:
    if newest is None:
        return "unknown age"
    delta = datetime.now(tz=timezone.utc) - newest
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "refreshed within the hour"
    if hours < 48:
        return f"refreshed {hours:.0f}h ago"
    return f"refreshed {hours / 24:.0f}d ago"


def apple_tv_health_check(ctx) -> HealthResult:
    """Snapshot the UTS cache and count parseable watch events.

    Returns ok=True when the cache opens and scans — even when zero watch
    events are found (that's a friendly nudge to watch something, not an
    error). Returns ok=False when the cache doesn't exist (TV app never
    ran), when the snapshot copy stalls, or when any other exception
    surfaces.
    """
    cache_dir = _resolve_cache_dir(ctx)
    try:
        scan = apple_tv.scan_cache(cache_dir)
    except apple_tv.SnapshotError as exc:
        return HealthResult(ok=False, summary=f"Cache snapshot failed: {exc}")
    except RuntimeError as exc:
        # iter_cache_entries' missing-cache error already tells the user
        # what to do ("open the TV app once").
        return HealthResult(ok=False, summary=str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return HealthResult(ok=False, summary=f"{type(exc).__name__}: {exc}")

    if scan.snapshot_count == 0:
        return HealthResult(
            ok=True,
            summary=(
                "Cache is readable but holds no Watch Now snapshots yet — "
                "open the TV app's Home tab once and re-check."
            ),
        )

    by_kind: dict[str, int] = {}
    for e in scan.events:
        kind = e.external_ids.get("kind", "?")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    if not scan.events:
        return HealthResult(
            ok=True,
            summary=(
                f"Cache is readable ({scan.snapshot_count} Watch Now "
                f"snapshot{'s' if scan.snapshot_count != 1 else ''}, "
                f"{_age_str(scan.newest_fetch)}) but no watch activity was "
                f"found — watch something in the TV app and re-check."
            ),
        )

    preview = [
        {"title": e.note, "watched_at": e.start_time.isoformat()}
        for e in sorted(scan.events, key=lambda e: e.start_time, reverse=True)[:3]
    ]
    parts = []
    for kind, label in (("continue", "in progress"),
                        ("completed_prior_episode", "completed"),
                        ("history", "from history")):
        if by_kind.get(kind):
            parts.append(f"{by_kind[kind]} {label}")
    return HealthResult(
        ok=True,
        summary=(
            f"Found {len(scan.events)} watch event"
            f"{'s' if len(scan.events) != 1 else ''} "
            f"({', '.join(parts)}) across {scan.snapshot_count} Watch Now "
            f"snapshot{'s' if scan.snapshot_count != 1 else ''}, "
            f"{_age_str(scan.newest_fetch)}."
        ),
        preview=preview,
    )
