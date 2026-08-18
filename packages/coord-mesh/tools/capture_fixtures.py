#!/usr/bin/env python3
"""Re-capture the live `fulcra-api` surfaces this package pins its argv against.

WHY THIS IS A SCRIPT AND NOT A HAND-EDIT. The r5 round died on a fixture whose
label was wrong: `real_share_create_help.txt` said it came from fulcra-api
0.1.40 and it came from 0.1.38, so a test asserting "`--file` does not exist"
passed here and was refuted on a reviewer's real 0.1.40. Nobody lied — the
version was never measured, only assumed, and an assumption written into a
docstring reads exactly like a measurement afterwards.

So a capture now records its own provenance, and it does so by MEASURING at
capture time rather than by asking the person running it. Provenance that a
human types is the same assumption in a new place.

Usage:
    python tools/capture_fixtures.py            # capture into tests/fixtures/
    python tools/capture_fixtures.py --check    # exit 1 if fixtures are stale

`--check` is the one to run before claiming a fixture reflects the installed
client: it re-measures and diffs without writing.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(HERE), "tests", "fixtures")
HELP_FIXTURE = os.path.join(FIXTURES, "real_share_create_help.json")


def _fulcra_cmd():
    return (os.environ.get("FULCRA_CMD") or "fulcra-api").split()


def measure_distribution_version(executable):
    """Version of the DISTRIBUTION that owns the executable we actually run.

    `fulcra-api` itself exposes no version surface — no `--version`, no
    `version` subcommand (measured 2026-08-18 on 0.1.40) — so the version has
    to come from the packaging metadata of the environment that owns the
    binary, not from the binary. Returns None when it cannot be established;
    a None here must never be rendered as a version.
    """
    path = None
    try:
        path = subprocess.run(["which", executable], capture_output=True,
                              text=True, timeout=20).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    # `uv tool list` is the installer of record for this fleet's CLIs.
    try:
        out = subprocess.run(["uv", "tool", "list"], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None, path
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == executable and parts[1].startswith("v"):
            return parts[1].lstrip("v"), path
    return None, path


def capture_help():
    argv = [*_fulcra_cmd(), "share", "create", "--help"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise SystemExit(f"capture failed rc={proc.returncode}: {proc.stderr[:400]}")
    version, path = measure_distribution_version(_fulcra_cmd()[0])
    return {
        "captured_from": _fulcra_cmd()[0],
        "executable_path": path,
        # MEASURED, not typed. None means "could not establish" and the tests
        # treat that as a failure rather than as an unversioned pass.
        "distribution_version": version,
        "surface": "fulcra-api share create --help",
        "help": proc.stdout,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="diff against the committed fixture; do not write")
    args = ap.parse_args()

    fresh = capture_help()
    if fresh["distribution_version"] is None:
        print("REFUSING: could not measure the installed fulcra-api version. "
              "An unlabelled capture is what broke r5.", file=sys.stderr)
        return 2

    if args.check:
        if not os.path.exists(HELP_FIXTURE):
            print(f"missing fixture {HELP_FIXTURE}", file=sys.stderr)
            return 1
        with open(HELP_FIXTURE, "r", encoding="utf-8") as fh:
            have = json.load(fh)
        drift = [k for k in ("distribution_version", "help")
                 if have.get(k) != fresh.get(k)]
        if drift:
            print(f"STALE fixture — differs in {drift}; committed version "
                  f"{have.get('distribution_version')!r}, installed "
                  f"{fresh['distribution_version']!r}", file=sys.stderr)
            return 1
        print(f"fixture current for fulcra-api {fresh['distribution_version']}")
        return 0

    os.makedirs(FIXTURES, exist_ok=True)
    with open(HELP_FIXTURE, "w", encoding="utf-8") as fh:
        json.dump(fresh, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"captured {HELP_FIXTURE} from fulcra-api {fresh['distribution_version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
