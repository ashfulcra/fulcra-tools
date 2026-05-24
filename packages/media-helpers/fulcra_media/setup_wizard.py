"""Interactive top-level `fulcra-media setup` walkthrough.

Drives the user through:
  1. Pick categories of media they want to track (multi-select).
  2. For each category, present the ranked pathway options.
  3. Show the wizard text for whichever option they pick.
  4. Echo the exact `fulcra-media import <cmd>` they should run next.

Data-driven from fulcra_media.service_catalog so adding a new service is
just a catalog entry, not a wizard edit.
"""
from __future__ import annotations

import click

from .service_catalog import (
    ServiceEntry,
    categories,
    services_for_category,
)


CATEGORY_LABELS = {
    "music": "Music (Spotify, Apple Music, Last.fm, ...)",
    "video": "TV / Movies (Netflix, Hulu, Disney+, Trakt, ...)",
    "podcasts": "Podcasts (Apple Podcasts, Spotify, ...)",
    "books": "Books / Reading",
    "self-hosted": "Self-hosted (Plex, Jellyfin)",
}


def _format_service(entry: ServiceEntry, idx: int) -> str:
    suffix = "" if entry.available else "  [planned]"
    return f"  {idx}. {entry.label}{suffix}\n      {entry.blurb}"


def _pick_category_indices(cats: list[str], echo) -> list[int]:
    """Read a comma-separated list of category numbers from stdin."""
    echo("\nWhich media services do you want to track? (comma-separated numbers)")
    for i, c in enumerate(cats, start=1):
        echo(f"  {i}. {CATEGORY_LABELS.get(c, c)}")
    raw = click.prompt("Categories", default="1,2", type=str)
    picks: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            n = int(piece)
        except ValueError:
            echo(f"  (ignoring {piece!r})")
            continue
        if 1 <= n <= len(cats):
            picks.add(n - 1)
    return sorted(picks)


def _walk_category(category: str, echo) -> ServiceEntry | None:
    """Show pathway options for a category, return the picked entry (or None)."""
    services = services_for_category(category)
    if not services:
        return None
    echo(f"\n→ For {CATEGORY_LABELS.get(category, category)}, ranked options:")
    for i, s in enumerate(services, start=1):
        echo(_format_service(s, i))
    raw = click.prompt(
        "Pick one (number, or 'skip')", default="1", type=str,
    )
    if raw.lower().strip() == "skip":
        return None
    try:
        n = int(raw)
    except ValueError:
        echo(f"  (couldn't parse {raw!r}, skipping)")
        return None
    if not (1 <= n <= len(services)):
        echo("  (out of range, skipping)")
        return None
    return services[n - 1]


def _explain_choice(entry: ServiceEntry, echo) -> None:
    """Print the wizard text + the exact import command."""
    if not entry.available:
        echo(f"\n  '{entry.label}' isn't implemented yet — but the pathway is researched.")
        echo(f"  Pathway: {entry.pathway}")
        echo("  See docs/superpowers/research/2026-05-17-media-service-pathways.md")
        return
    echo(f"\n  Setting up {entry.label} ({entry.pathway} pathway)\n")
    if entry.wizard:
        echo(f"  Walkthrough: fulcra-media wizard {entry.wizard}")
    if entry.import_cmd:
        echo(f"  Run when ready: fulcra-media import {entry.import_cmd}")


@click.command("setup", help="Interactive picker that walks you through service onboarding.")
def setup() -> None:
    """Top-level fulcra-media setup wizard."""
    echo = click.echo
    echo("fulcra-media setup\n")
    echo("This walks you through picking media services to import. You can")
    echo("rerun this any time — it doesn't change state, just shows you the")
    echo("right wizard and import command for each service.\n")

    cats = categories()
    picks = _pick_category_indices(cats, echo)
    if not picks:
        echo("\nNo categories picked. Exit.")
        return

    for idx in picks:
        cat = cats[idx]
        entry = _walk_category(cat, echo)
        if entry is None:
            continue
        _explain_choice(entry, echo)

    echo("\nDone. To see all available wizards:  fulcra-media wizard --help")
    echo("To see all import commands:           fulcra-media import --help")
