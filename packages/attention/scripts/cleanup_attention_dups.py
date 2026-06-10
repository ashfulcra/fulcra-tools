#!/usr/bin/env python3
"""Clean up the attention duplicate-record storm residue.

Background (docs/audits/2026-06-09-collect-attention-real-data-audit.md):
the pre-relayless attention pipeline ingested the SAME visit record up to
2,729 times — 144,664 of 149,789 attention records (97%) in the Apr 10 –
Jun 1 2026 window are exact identical-timestamp clones of 5,125 real
visits. The current v3 relayless path is clean; this is bounded legacy
residue.

This tool:
  1. fetches DurationAnnotation records over the residue window (chunked),
  2. groups them by attention source_id (``com.fulcra.attention.*``),
  3. keeps exactly ONE record per source_id (deterministic: lowest
     (start_time, id)), and
  4. writes a manifest of every excess clone's record id —
     then, ONLY with ``--execute``, deletes the excess via a pluggable
     delete transport.

The per-event delete capability is being added to the fulcra-api CLI (a
branch pending fold-in to main at the time of writing), so the delete
seam is pluggable rather than hardcoded:

  --via cli   shells out per id to a command template, e.g.
                --cli-delete-cmd "fulcra record delete {id}"
              (defaults to autodetecting a delete-capable subcommand from
              ``fulcra --help`` once the branch lands; refuses to guess
              if none is found).
  --via api   issues ``DELETE <--endpoint with {id}>`` directly with the
              bearer token from ``fulcra auth print-access-token`` (or
              $FULCRA_ACCESS_TOKEN). Use once the live endpoint shape is
              known, e.g.:
                --endpoint "https://api.fulcradynamics.com/data/v1alpha1/event/{id}"

DRY-RUN IS THE DEFAULT. Without ``--execute`` nothing is deleted; the
manifest + summary are written so the deletion set can be reviewed (or
handed to the backend team).

Safety rails:
  - the keep-set and delete-set are asserted disjoint;
  - refuses to run when >0 records would be deleted for a source_id whose
    clones do NOT share identical recorded_at timestamps (that would mean
    the "exact clone" premise is wrong for that visit — those are listed
    and skipped instead);
  - ``--yes`` required for more than 100 deletions;
  - stops after 5 consecutive delete failures;
  - 404/410 on delete counts as already-gone (idempotent resume);
  - progress journal lets a killed run resume without re-deleting;
  - never touches records that carry no com.fulcra.attention.* source.

Usage:
  # 1. dry-run: build + review the manifest
  uv run --project packages/attention python packages/attention/scripts/cleanup_attention_dups.py \
      --manifest /tmp/attention-dups.json

  # 2. execute via the CLI delete (after the branch folds into main)
  ... --execute --via cli --cli-delete-cmd "fulcra record delete {id}" --yes

  # 3. or execute via the raw endpoint
  ... --execute --via api \
      --endpoint "https://api.fulcradynamics.com/data/v1alpha1/event/{id}" --yes

  # 4. verify afterwards
  ... --verify
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("attention-dup-cleanup")

API_BASE = "https://api.fulcradynamics.com"
EVENT_PATH = "/data/v1alpha1/event/DurationAnnotation"
ATTENTION_PREFIX = "com.fulcra.attention."

# Residue bounds from the audit (no attention records exist before April;
# the storm stopped at the 2026-06-01/02 relayless cutover). A margin on
# both sides costs little and catches stragglers.
DEFAULT_START = "2026-04-01T00:00:00Z"
DEFAULT_END = "2026-06-05T00:00:00Z"

# The event endpoint has an undocumented pagination ceiling (~4k records,
# no cursor). Storm days carry ~20k records, so chunk small and treat a
# suspiciously-round chunk as possibly truncated.
CHUNK_DAYS = 1
TRUNCATION_SUSPECT = 4000

MAX_CONSECUTIVE_FAILURES = 5
YES_THRESHOLD = 100


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def _bearer_token() -> str:
    tok = os.environ.get("FULCRA_ACCESS_TOKEN")
    if tok:
        return tok.strip()
    out = subprocess.run(
        ["fulcra", "auth", "print-access-token"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(
            "no bearer token: set FULCRA_ACCESS_TOKEN or sign in via "
            "`fulcra auth login`"
        )
    return out.stdout.strip().splitlines()[-1]


def _http(method: str, url: str, token: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iter_chunks(start: datetime, end: datetime, days: int = CHUNK_DAYS):
    """Yield [lo, hi) windows of ``days`` covering [start, end)."""
    lo = start
    step = timedelta(days=days)
    while lo < end:
        hi = min(lo + step, end)
        yield lo, hi
        lo = hi


def fetch_window(token: str, start: datetime, end: datetime,
                 chunk_days: int = CHUNK_DAYS) -> list[dict]:
    """Fetch DurationAnnotation records for [start, end), chunked."""
    records: list[dict] = []
    for lo, hi in iter_chunks(start, end, days=chunk_days):
        url = (
            f"{API_BASE}{EVENT_PATH}"
            f"?start_time={_iso(lo)}&end_time={_iso(hi)}"
        )
        status, body = _http("GET", url, token)
        if status != 200:
            raise RuntimeError(f"fetch {lo:%Y-%m-%d}: HTTP {status}")
        chunk = _parse_records(body)
        if len(chunk) >= TRUNCATION_SUSPECT:
            log.warning(
                "chunk %s..%s returned %d records — possible pagination "
                "truncation; re-run with a smaller chunk via --chunk-days",
                _iso(lo), _iso(hi), len(chunk),
            )
        log.debug("chunk %s: %d records", _iso(lo), len(chunk))
        records.extend(chunk)
    return records


def _parse_records(body: bytes) -> list[dict]:
    """The endpoint returns NDJSON (one record per line) or a JSON array."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# --------------------------------------------------------------------------
# Dedup planning (pure logic — unit-tested)
# --------------------------------------------------------------------------

@dataclass
class Plan:
    total_attention_records: int = 0
    keep: dict[str, str] = field(default_factory=dict)      # source_id -> record id
    delete: list[str] = field(default_factory=list)          # record ids
    skipped_nonclone: dict[str, list[str]] = field(default_factory=dict)
    by_day: Counter = field(default_factory=Counter)
    by_version: Counter = field(default_factory=Counter)


def attention_sources_of(record: dict) -> list[str]:
    return sorted({
        str(s) for s in record.get("sources") or []
        if str(s).startswith(ATTENTION_PREFIX)
    })


def build_plan(records: list[dict]) -> Plan:
    """Group records into visits; keep one record per visit.

    A record can carry SEVERAL attention sources (live-data shape: the
    same record lists both a v1 and a v2 fingerprint), and clones of one
    visit may list overlapping-but-unequal source sets. Grouping by any
    single source therefore splits a visit across groups — caught by the
    disjointness rail on real data. Visits are instead the CONNECTED
    COMPONENTS over shared attention sources (union-find, deterministic
    min-source root).

    Keep choice is deterministic: lowest (start_time, record id). Excess
    records go to the delete list ONLY when they share the keeper's
    identical recorded_at (the verified storm shape); records with
    diverging timestamps are reported in ``skipped_nonclone`` and left
    alone.
    """
    plan = Plan()

    # Union-find over attention source ids; root = min source in component.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        lo, hi = (ra, rb) if ra < rb else (rb, ra)
        parent[hi] = lo

    rec_sources: list[tuple[dict, list[str]]] = []
    seen_ids: set[str] = set()
    for r in records:
        srcs = attention_sources_of(r)
        if not srcs or not r.get("id"):
            continue
        # Chunked fetching returns a midnight-spanning record in BOTH
        # adjacent chunks (the API matches by interval overlap) — without
        # this, a record becomes its own clone and trips the keep/delete
        # disjointness rail.
        if r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        for s in srcs:
            parent.setdefault(s, s)
        for s in srcs[1:]:
            union(srcs[0], s)
        rec_sources.append((r, srcs))

    groups: dict[str, list[dict]] = defaultdict(list)
    for r, srcs in rec_sources:
        groups[find(srcs[0])].append(r)

    for sid, recs in groups.items():
        plan.total_attention_records += len(recs)
        recs_sorted = sorted(
            recs,
            key=lambda r: (
                (r.get("recorded_at") or {}).get("start_time") or "",
                r.get("id") or "",
            ),
        )
        keeper = recs_sorted[0]
        plan.keep[sid] = keeper["id"]
        if len(recs_sorted) == 1:
            continue
        keeper_at = json.dumps(keeper.get("recorded_at") or {}, sort_keys=True)
        clones, diverged = [], []
        for r in recs_sorted[1:]:
            at = json.dumps(r.get("recorded_at") or {}, sort_keys=True)
            (clones if at == keeper_at else diverged).append(r["id"])
        if diverged:
            plan.skipped_nonclone[sid] = diverged
        plan.delete.extend(clones)
        day = ((keeper.get("recorded_at") or {}).get("start_time") or "")[:10]
        ver = sid[len(ATTENTION_PREFIX):].split(".")[0]
        plan.by_day[day] += len(clones)
        plan.by_version[ver] += len(clones)

    keep_ids = set(plan.keep.values())
    overlap = keep_ids & set(plan.delete)
    if overlap:  # structurally impossible; assert anyway (safety rail)
        raise AssertionError(f"keep/delete overlap: {sorted(overlap)[:3]}")
    return plan


# --------------------------------------------------------------------------
# Delete transports (the pluggable seam)
# --------------------------------------------------------------------------

def detect_cli_delete_cmd() -> str | None:
    """Autodetect a per-record delete subcommand on the installed fulcra CLI.

    The capability ships on a fulcra-api branch pending fold-in to main;
    until the installed CLI has it, this returns None and --via cli
    refuses to guess.
    """
    candidates = [
        ("record", "delete", "fulcra record delete {id}"),
        ("event", "delete", "fulcra event delete {id}"),
        ("annotation", "delete-record", "fulcra annotation delete-record {id}"),
    ]
    for group, sub, template in candidates:
        out = subprocess.run(
            ["fulcra", group, "--help"], capture_output=True, text=True,
        )
        if out.returncode == 0 and sub in out.stdout:
            return template
    return None


def delete_via_cli(record_id: str, template: str) -> bool:
    """Run the delete command for one id. True = gone (incl. already-gone)."""
    cmd = [part.format(id=record_id) for part in shlex.split(template)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if out.returncode == 0:
        return True
    blob = (out.stdout + out.stderr).lower()
    if "404" in blob or "not found" in blob or "410" in blob:
        return True  # already gone — idempotent resume
    log.error("cli delete %s failed rc=%d: %s",
              record_id, out.returncode, (out.stderr or out.stdout)[:200])
    return False


def delete_via_api(record_id: str, endpoint_template: str, token: str) -> bool:
    url = endpoint_template.format(id=record_id)
    status, body = _http("DELETE", url, token)
    if status in (200, 202, 204):
        return True
    if status in (404, 410):
        return True  # already gone
    log.error("api delete %s -> HTTP %d %s", record_id, status, body[:200])
    return False


def run_deletes(ids: list[str], deleter, journal_path: Path,
                rate_per_sec: float = 10.0) -> tuple[int, int]:
    """Delete ids with resume journal + consecutive-failure stop.

    Returns (deleted, failed).
    """
    done: set[str] = set()
    if journal_path.exists():
        done = set(journal_path.read_text().split())
        log.info("resume: %d ids already deleted per journal", len(done))
    deleted = failed = consecutive = 0
    interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
    with journal_path.open("a") as journal:
        for rid in ids:
            if rid in done:
                continue
            ok = deleter(rid)
            if ok:
                deleted += 1
                consecutive = 0
                journal.write(rid + "\n")
                journal.flush()
            else:
                failed += 1
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "stopping: %d consecutive failures (deleted=%d, "
                        "failed=%d) — journal allows safe resume",
                        consecutive, deleted, failed,
                    )
                    break
            if interval:
                time.sleep(interval)
    return deleted, failed


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def summarize(plan: Plan) -> str:
    lines = [
        f"attention records in window: {plan.total_attention_records}",
        f"distinct visits (kept):      {len(plan.keep)}",
        f"excess clones to delete:     {len(plan.delete)}",
        f"non-clone groups skipped:    {len(plan.skipped_nonclone)}",
        "by wire version: " + ", ".join(
            f"{v}={c}" for v, c in sorted(plan.by_version.items())),
        "top days: " + ", ".join(
            f"{d}={c}" for d, c in plan.by_day.most_common(6)),
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--chunk-days", type=int, default=CHUNK_DAYS)
    ap.add_argument("--manifest", type=Path,
                    default=Path("/tmp/attention-dups.json"))
    ap.add_argument("--journal", type=Path,
                    default=Path("/tmp/attention-dups.deleted.journal"))
    ap.add_argument("--execute", action="store_true",
                    help="actually delete (default: dry-run manifest only)")
    ap.add_argument("--verify", action="store_true",
                    help="re-scan the window and report remaining dups")
    ap.add_argument("--via", choices=("cli", "api"), default="cli")
    ap.add_argument("--cli-delete-cmd", default=None,
                    help='e.g. "fulcra record delete {id}"')
    ap.add_argument("--endpoint", default=None,
                    help='e.g. "https://api.fulcradynamics.com/data/'
                         'v1alpha1/event/{id}"')
    ap.add_argument("--rate", type=float, default=10.0,
                    help="deletes per second (default 10)")
    ap.add_argument("--yes", action="store_true",
                    help=f"required when deleting more than {YES_THRESHOLD}")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = _bearer_token()
    start, end = _parse_iso(args.start), _parse_iso(args.end)

    log.info("scanning %s .. %s", args.start, args.end)
    records = fetch_window(token, start, end, chunk_days=args.chunk_days)
    plan = build_plan(records)
    print(summarize(plan))

    if args.verify:
        if plan.delete:
            print(f"\nVERIFY: {len(plan.delete)} excess clones REMAIN")
            return 1
        print("\nVERIFY: clean — one record per visit")
        return 0

    args.manifest.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": args.start, "end": args.end},
        "total_attention_records": plan.total_attention_records,
        "distinct_visits": len(plan.keep),
        "delete_count": len(plan.delete),
        "skipped_nonclone": plan.skipped_nonclone,
        "by_day": dict(plan.by_day),
        "by_version": dict(plan.by_version),
        "delete": plan.delete,
    }, indent=1))
    log.info("manifest written: %s", args.manifest)

    if not args.execute:
        print(f"\nDRY-RUN: no deletions. Review {args.manifest}, then re-run "
              f"with --execute --via cli|api (see --help).")
        return 0

    if len(plan.delete) > YES_THRESHOLD and not args.yes:
        print(f"refusing to delete {len(plan.delete)} records without --yes")
        return 2

    if args.via == "cli":
        template = args.cli_delete_cmd or detect_cli_delete_cmd()
        if not template:
            print("no per-record delete subcommand found on the installed "
                  "fulcra CLI and no --cli-delete-cmd given. The delete "
                  "capability ships on a fulcra-api branch pending fold-in "
                  "to main — once `fulcra <record|event> delete` exists, "
                  "re-run (or pass --cli-delete-cmd / use --via api).")
            return 3
        log.info("deleting via CLI template: %s", template)
        deleter = lambda rid: delete_via_cli(rid, template)  # noqa: E731
    else:
        if not args.endpoint or "{id}" not in args.endpoint:
            print("--via api requires --endpoint containing {id}")
            return 3
        log.info("deleting via endpoint: %s", args.endpoint)
        deleter = lambda rid: delete_via_api(rid, args.endpoint, token)  # noqa: E731

    deleted, failed = run_deletes(
        plan.delete, deleter, args.journal, rate_per_sec=args.rate)
    print(f"\ndeleted={deleted} failed={failed} "
          f"(journal: {args.journal} — re-run to resume; "
          f"then re-run with --verify)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
