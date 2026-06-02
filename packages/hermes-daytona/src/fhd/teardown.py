"""List or delete guest sandboxes (those labelled fhd=guest)."""
from __future__ import annotations
import argparse

from daytona import Daytona, DaytonaConfig

from fhd.config import load_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="List/delete guest Hermes sandboxes")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="List guest sandboxes")
    g.add_argument("--delete", metavar="ID", help="Delete one sandbox by id")
    g.add_argument("--all", action="store_true", help="Delete ALL guest sandboxes")
    return ap.parse_args(argv)


def _guest_sandboxes(d: Daytona) -> list:
    """All sandboxes spawned by this tool (labelled fhd=guest). d.list() is a
    generator, so materialize it."""
    return [sb for sb in d.list() if getattr(sb, "labels", {}).get("fhd") == "guest"]


def main() -> None:
    args = parse_args()
    s = load_settings()
    d = Daytona(
        DaytonaConfig(
            api_key=s.daytona_api_key,
            api_url=s.daytona_api_url,
            target=s.daytona_target,
        )
    )

    if args.list:
        guests = _guest_sandboxes(d)
        for sb in guests:
            label = getattr(sb, "labels", {}).get("guest", "?")
            print(f"{sb.id}  guest={label}  state={getattr(sb, 'state', '?')}")
        print(f"{len(guests)} guest sandbox(es).")
        return

    if args.delete:
        d.get(args.delete).delete()
        print(f"Deleted {args.delete}")
        return

    if args.all:
        guests = _guest_sandboxes(d)
        for sb in guests:
            sb.delete()
            print(f"Deleted {sb.id}")
        print(f"Deleted {len(guests)} guest sandbox(es).")


if __name__ == "__main__":
    main()
