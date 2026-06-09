# fulcra-coord 0.13.0 — deploy / rollout runbook

Audience: the operator (Ash) + teammates running the fleet. This is the
copy-pasteable upgrade + verification path for the 0.13.0 release. It is blunt on
purpose — read the "honest" lines, not just the commands.

## 1. What 0.13.0 is

0.13.0 is the **event-sourcing substrate**: alongside the mutable
`tasks/<id>.json` files the bus has always written, every task mutation now also
appends one immutable shard to an append-only event log
(`events/tasks/<id>/<event_id>.json`). A `fold_task` reducer can replay those
shards back into a task, and `FULCRA_COORD_READ_SOURCE` can (per host) cut reads
over to that folded view.

It is **additive and default-safe**:

- The event-log **dual-write is best-effort and default-ON** — an append failure
  is logged but never fails the task mutation. It just starts accumulating.
- **Reads still default to `file`** (`FULCRA_COORD_READ_SOURCE=file`). Upgrading
  changes **no read behaviour** — a 0.13.0 host returns byte-identical reads to a
  0.12.0 host until someone explicitly opts that host into `events`.
- The read **flip is a SEPARATE, later operator decision** (section 5), gated on
  parity, fully reversible per host, and never automatic.

No breaking changes. Upgrading the fleet is safe.

## 2. Why deploy now

The migration is **merged but dark**. Main carries all the substrate code (event
dual-write, `fold_task`/snapshot, the flag-gated read cutover, the reconcile
parity safety net, retention, the additive Directive record) — but it merged
**without a version bump**, so until now a deployed-old CLI couldn't be told from
a deployed-new one. As of this release `__version__` is `0.13.0` and
`fulcra-coord --version` is meaningful again.

Right now the substrate is real in the code and unreal on the bus: **zero
`events/` shards exist on the live coordination root**, because the whole fleet
is still running pre-migration 0.12.0 and no host is dual-writing. Deploying is
what makes the substrate real:

- it starts the **dual-write accumulating** an event log, and
- it starts the **parity-validation clock** (reconcile begins recording
  `event_parity` per host).

Nothing downstream — the read flip, any future phase — is real or validatable
until this lands on the fleet. So this deploy is the prerequisite for everything
that follows, while changing nothing about how the fleet behaves today.

## 3. Per-host upgrade

The fleet: **Mac, Ashs-MBP-Work, ArcBot, DeskbookPro.** The CLI is installed via
`uv tool` and lives at `~/.local/bin/fulcra-coord`. Upgrade each host from a clean
checkout of the package directory (this is the method ONBOARD.md documents):

```bash
# from the fulcra-coord package dir on each host
git pull
uv tool install --reinstall --force .
```

`--reinstall --force` is required, not optional: `uv` skips the rebuild when the
version is unchanged, which is exactly what silently froze older installs at a
stale subcommand set. (Because we bumped the version this time, a plain reinstall
would also rebuild — but keep the flags as the durable habit.)

Then restart that host's inbox listener so the running launchd/cron job picks up
the new binary:

```bash
fulcra-coord install-listener --agent <this-host's-agent-id>
```

`install-listener` is idempotent and re-points the per-agent launchd LaunchAgent
(macOS) / crontab line (elsewhere) at the freshly installed CLI. If the host also
runs the reconcile heartbeat, re-run `fulcra-coord install-heartbeat` the same
way.

Verify the upgrade took — this check is now meaningful because we bumped the
version:

```bash
fulcra-coord --version    # must report 0.13.0
```

If it still reports 0.12.0, the reinstall didn't replace the on-PATH binary —
check that `~/.local/bin` is ahead of any other `fulcra-coord` on `PATH`.

## 4. Post-deploy verification

Once at least one upgraded (0.13.0) host has done real task mutations
(`start` / `update` / `done` / `block`), two things should appear that are absent
today:

**(a) The event tree starts filling.** An `events/tasks/<id>/` subtree appears
under the coordination root and shards accumulate, one immutable file per
mutation:

```bash
# default root is /coordination; substitute FULCRA_COORD_REMOTE_ROOT if overridden
fulcra-api file list /coordination/events/tasks/
# then drill into a specific task to see the shards pile up
fulcra-api file list /coordination/events/tasks/TASK-<id>/
```

Today that path is empty (no host dual-writes). Seeing shards is the dual-write
proving it works end to end.

**(b) The reconcile parity record appears.** On an upgraded host, run reconcile,
then read that host's health record:

```bash
fulcra-coord reconcile
fulcra-api file download /coordination/health/<host>.json   # then read it
```

The health record should now carry an `event_parity` block:

```json
"event_parity": {
  "checked": <n>,
  "drift": <n>,
  "drift_task_ids": [...],
  "ack_drift": <n>,
  "ack_drift_task_ids": [...]
}
```

`event_parity` is **absent today** — no host runs the parity check yet, because
no host is on 0.13.0. Its appearance confirms reconcile is folding each task's
event log and comparing it against the mutable file. The check is **report-only**:
the mutable file stays authoritative and nothing is rewritten. `drift` counts
tasks where the fold disagrees with the file; `ack_drift` separately counts folds
missing a durable ack the `summaries` view still holds (see section 5).

## 5. Then: staged validation → flip

Deploying 0.13.0 does **not** flip the read path — it only starts the dual-write
and the parity clock. The flip is a separate, deliberate, staged path. (See the
flip-readiness runbook at
`docs/superpowers/specs/2026-06-09-read-cutover-flip-readiness.md` if present; the
inline summary below stands on its own.)

1. **Opt ONE host into `events`** (reversible — just unset the var to revert):

   ```bash
   FULCRA_COORD_READ_SOURCE=events fulcra-coord status
   # or persist it for that one host's environment to validate over time
   ```

   Under `events`, reads use the folded view **only** when the fold is a complete
   snapshot, and fall back to the mutable file on a delta-only / empty / errored
   fold — so this is incompleteness-and-error-safe by construction. Any
   unrecognised value silently degrades to `file`.

2. **Watch parity for a sustained window.** Keep running reconcile and watching
   `event_parity` in the health records across the fleet. You want **`drift ==
   0`** held over a real window — not a single green tick.

3. **`ack_drift` is EXPECTED to be nonzero until ack durability lands (Phase
   3b).** A delta-only or truncated event log can lose an `inbox_ack` that the
   aggregate `summaries` view still holds, so the fold reports an ack the file
   path wouldn't. This is a known gap, it correctly **blocks the flip**, and it is
   NOT a regression to chase down now — it resolves when Phase 3b makes acks
   durable in the event log. Watch `drift`; tolerate `ack_drift`.

4. **Flipping the FLEET default is the operator's separate, deliberate call.**
   Never automatic, never a side effect of this deploy. It happens only after
   sustained `drift == 0`, and it stays fully reversible per host (unset
   `FULCRA_COORD_READ_SOURCE` to fall back to byte-identical file reads).

Until that flip, the mutable file is the source of truth and `events` is opt-in
per host.
