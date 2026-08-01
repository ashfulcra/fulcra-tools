# The rollback probe — externally-verifiable evidence of container filesystem rollback

Cloud agent sessions in this repo's coordination pattern report "container was
restarted" events and have repeatedly *observed* local files reverting to
earlier states (stale script copies with already-landed patches missing, an
engine binary reverting to a pre-bus-v3 version that wrote unreadable events).
All of that evidence was **self-diagnosed by the same filesystem being
questioned** — reasonable grounds for skepticism.

This probe replaces self-diagnosis with tamper-evident external history. It
implements the experiment proposed by the Fulcra data-engineering team
(Michael's append+external-log design, upgraded per Greg with `git bundle`
verification).

## Method

Every tick (default 120s), `scripts/coord-boss/rollback-probe.sh`:

1. **Verifies before writing.** Downloads the external `latest.json` and
   compares it against the local probe state — the append file's last `seq`
   and whether the externally-recorded git HEAD exists in the local repo.
2. **Appends locally.** `seq=N ts=<iso> boot=<boot_id>` to a local file, and
   commits it to a local git repo (one commit per tick).
3. **Logs externally, three ways.**
   - an **immutable Fulcra record row** `{"probe":"rollback","seq":N,"ts":…,
     "boot":…,"head":…}` — the monotonic, timestamped external log
     (Michael's design; rows cannot be rewritten by the container);
   - a **`git bundle` of the full repo history** uploaded to the File Store
     (Greg's design — the authoritative external repo history);
   - a small `latest.json` (seq, head, ts, boot) for cheap verification.

## What a detection means

On any tick where the local filesystem is **behind** the external history:

| kind | meaning |
|---|---|
| `rollback` | local file/repo present but missing writes the external log proves happened — the filesystem reverted |
| `wiped` | local probe state absent entirely — fresh scratchpad, not a snapshot revert |
| `write-gap` | local is *ahead* of external — an earlier upload failed; **not** a rollback |

Incidents upload to
`team/fulcra/_coord/agents/coord-boss/rollback-probe/incidents/<ts>-<kind>.json`
with both histories' positions, timestamps, and the container `boot_id` —
enough to say *that* it happened, *when*, and across which boot boundary. An
incident record row is also written so the event is queryable next to the
tick log.

The `boot_id` matters: a genuine snapshot revert shows an **older seq under a
new boot id while the external log continues past it** — unambiguous, because
the external rows are written over the network at append time and cannot be
regressed by anything the container does.

## Detection self-test

The probe's detection path was proven by simulation on 2026-08-01: local log
truncated + `git reset --hard HEAD~1`, next tick correctly reported
`state=rollback` and filed the incident. That artifact is preserved at
`rollback-probe/selftest-2026-08-01.json` (relabeled so it can never be
mistaken for a real event); `incidents/` contains only real detections.

## Reading the results

- Live tick log: `fulcra get-records <bus-annotation-type> "24h"` filtered to
  `"probe":"rollback"` — gaps in `seq` between boots are restarts; a `seq`
  that *repeats* under a later boot is a rollback.
- Verdict at any moment: `bash scripts/coord-boss/rollback-probe.sh verify`
  → `ok | wiped | rollback | write-gap | first-run`.
- Evidence directory: `_coord/agents/coord-boss/rollback-probe/` in the
  team store.

Null result matters too: if weeks pass with restarts observed (wiped states,
new boot ids) but zero `rollback` incidents, then "files reverted" was
misattributed — most likely to environment-snapshot restore of the scratchpad
or to stale *store* copies being faithfully re-downloaded — and the field
manual's claims will be corrected to say exactly that.
