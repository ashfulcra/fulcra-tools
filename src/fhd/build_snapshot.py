"""Build/register the reusable Daytona snapshot. Run once (and after image changes).

This is the slow step (a few minutes): Daytona builds the image server-side from
the declarative definition in fhd.image and registers it as a named Snapshot that
spawn.py then instantiates per guest.
"""
from __future__ import annotations
import argparse
import time

from daytona import Daytona, DaytonaConfig, CreateSnapshotParams

from fhd.config import load_settings
from fhd.image import build_image

SNAPSHOT_NAME = "fhd-hermes-demo"


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Hermes demo snapshot")
    ap.add_argument("--name", default=SNAPSHOT_NAME)
    args = ap.parse_args()

    s = load_settings()
    d = Daytona(
        DaytonaConfig(
            api_key=s.daytona_api_key,
            api_url=s.daytona_api_url,
            target=s.daytona_target,
        )
    )
    # Idempotent rebuild: a same-named snapshot (incl. a failed/partial one from a
    # prior aborted build) blocks creation, so delete it and wait for the deletion
    # to finalize (delete is async) before recreating.
    try:
        existing = d.snapshot.get(args.name)
        print(f"Deleting existing snapshot '{args.name}' (state: {existing.state}) ...")
        d.snapshot.delete(existing)
        for _ in range(30):
            try:
                d.snapshot.get(args.name)
                time.sleep(2)
            except Exception:
                break  # gone
        print("Deleted.")
    except Exception:
        pass  # not found -> nothing to delete

    snap = d.snapshot.create(
        CreateSnapshotParams(name=args.name, image=build_image()),
        on_logs=lambda c: print(c, end=""),
    )
    print(f"\nSnapshot '{args.name}' state: {snap.state}")


if __name__ == "__main__":
    main()
