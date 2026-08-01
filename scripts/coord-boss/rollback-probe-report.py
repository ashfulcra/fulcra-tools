#!/usr/bin/env python3
"""Render the rollback-probe evidence into a trace Michael can inspect.

Reads ONLY external evidence — the immutable Fulcra record rows (the
over-the-network append log) and the store-side incidents/bundle — never the
container's local files, so the report cannot be polluted by the thing under
test. Output: a markdown report with (1) a boot/sequence timeline, (2) every
restart boundary and what happened to the sequence across it, (3) verbatim
incident docs, (4) the raw rows, untruncated, as an appendix.

Usage: rollback-probe-report.py [DAYS] > report.md   (default 7)
Requires: fulcra-api CLI authed; jq-free (stdlib only).
"""
import json, subprocess, sys, tempfile, os, signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # clean exit when piped to head

TYPE = "MomentAnnotation/ea49d0d3-acb7-49c6-93b6-bee81d126c92"
STORE = "team/fulcra/_coord/agents/coord-boss/rollback-probe"
DAYS = sys.argv[1] if len(sys.argv) > 1 else "7"


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def rows():
    r = sh("fulcra", "get-records", TYPE, f"{DAYS} days")
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        note = rec.get("note")
        if not isinstance(note, str):
            continue
        try:
            payload = json.loads(note)
        except ValueError:
            continue
        if isinstance(payload, dict) and str(payload.get("probe", "")).startswith("rollback"):
            payload["_recorded_at"] = rec.get("recorded_at")
            payload["_record_id"] = rec.get("id")
            out.append(payload)
    out.sort(key=lambda p: str(p.get("_recorded_at") or ""))
    return out


def store_list(path):
    r = sh("fulcra-api", "file", "list", path)
    return [l.split()[-1] for l in r.stdout.splitlines() if l.strip() and not l.strip().endswith("/")]


def store_read(path):
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    try:
        r = sh("fulcra-api", "file", "download", path, tmp)
        if r.returncode != 0:
            return None
        with open(tmp) as fh:
            return fh.read()
    finally:
        os.unlink(tmp)


def main():
    ticks = rows()
    print(f"# Rollback-probe evidence report — last {DAYS} days")
    print(f"\nGenerated from EXTERNAL evidence only: {len(ticks)} immutable record "
          "rows (the over-the-network append log) plus store-side incident docs. "
          "The container's local files were not consulted.\n")
    print("Method: `docs/coord/rollback-probe.md`. Live store evidence: "
          f"`{STORE}/` (latest.json, repo.bundle, incidents/).\n")

    # --- timeline by boot ---
    print("## Boot / sequence timeline\n")
    print("| # | boot_id (8) | first seq | last seq | ticks | first ts | last ts |")
    print("|---|---|---|---|---|---|---|")
    boots, order = {}, []
    for p in ticks:
        b = str(p.get("boot", "?"))[:8]
        if b not in boots:
            boots[b] = []
            order.append(b)
        boots[b].append(p)
    for i, b in enumerate(order, 1):
        ps = [p for p in boots[b] if "seq" in p]
        if not ps:
            continue
        print(f"| {i} | `{b}` | {ps[0]['seq']} | {ps[-1]['seq']} | {len(ps)} "
              f"| {ps[0].get('ts','?')} | {ps[-1].get('ts','?')} |")

    # --- restart boundaries ---
    print("\n## Restart boundaries (the evidence that matters)\n")
    if len(order) < 2:
        print("*No boot transition captured yet in this window — the probe has "
              "not yet lived across a container restart.*")
    for a, b in zip(order, order[1:]):
        pa = [p for p in boots[a] if "seq" in p]
        pb = [p for p in boots[b] if "seq" in p]
        if not pa or not pb:
            continue
        last_a, first_b = pa[-1], pb[0]
        state = first_b.get("state", "?")
        verdict = {
            "wiped": "scratchpad ABSENT at new boot (fresh container) — "
                     "external log carried the history forward",
            "rollback": "LOCAL STATE REVERTED — local was BEHIND the external "
                        "log at new boot: a genuine filesystem rollback",
            "ok": "local state INTACT across the restart (no loss)",
            "first-run": "probe state absent (treated as fresh start)",
            "write-gap": "local ahead of external (an earlier upload failed)",
        }.get(state, state)
        print(f"- boot `{a}` (last seq {last_a['seq']} at {last_a.get('ts')}) → "
              f"boot `{b}` (first tick state=`{state}`, resumed seq "
              f"{first_b['seq']} at {first_b.get('ts')}): **{verdict}**")

    print("\n> **Self-test window:** rows timestamped 2026-08-01T14:02:31Z through "
          "14:03:07Z (seq 1-3, including the seq-3 `state=rollback` tick and the "
          "`rollback-INCIDENT` row) are the DETECTION SELF-TEST — a deliberately "
          "simulated rollback proving the probe catches divergence. They are "
          "permanently in the log precisely because record rows cannot be deleted "
          "or rewritten; that immutability is the property the whole experiment "
          "rests on. Every row after that window is live, unsimulated evidence.\n")

    # --- incidents, verbatim ---
    print("\n## Incident documents (verbatim)\n")
    incs = store_list(f"{STORE}/incidents/")
    if not incs:
        print("*(none — no divergence detected so far; the self-test artifact "
              f"lives separately at `{STORE}/selftest-2026-08-01.json`)*")
    for name in incs:
        body = store_read(f"{STORE}/incidents/{name}")
        print(f"### `{name}`\n\n```json\n{(body or '(unreadable)').strip()}\n```\n")

    # --- raw rows appendix ---
    print("\n## Appendix: raw external record rows (untruncated)\n")
    print("One JSON object per line; `_recorded_at`/`_record_id` are the "
          "platform's own ingestion stamps — not writable by the probe.\n")
    print("```jsonl")
    for p in ticks:
        print(json.dumps(p, sort_keys=True))
    print("```")


if __name__ == "__main__":
    main()
