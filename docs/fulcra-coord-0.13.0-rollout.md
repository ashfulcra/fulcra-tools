## 0.14.0 — what's new since 0.13.0

0.14.0 is **purely additive** on top of the 0.13.0 event-sourcing substrate. It
adds observability, retention, and the Phase 3b directive dual-write — and it
changes nothing about how reads resolve. There is **still no read-cutover flip**:
the flip stays a separate, operator-gated decision (section 5 below), and a
0.14.0 host returns byte-identical reads to a 0.13.0 host until someone opts that
host into `events`.

**Upgrade procedure is unchanged from 0.13.0.** Follow section 3 exactly —
`git pull && uv tool install --reinstall --force .` per host, then re-point the
listener/heartbeat. Then `fulcra-coord --version` should report `0.14.0`.

### What 0.14.0 adds

Three feature lines merged after the 0.13.0 bump (#123, #124, #125) without their
own version bump; 0.14.0 makes them a deployable, detectable release. All three
are additive and default-safe:

- **Event-liveness observability (#123).** Reconcile now emits parity *coverage*
  and fold-*completeness* counts, a fold-read-error signal, and a dual-write
  append-failure count. These let you see whether the event log is actually
  keeping up, not just whether the tasks it covers agree.
- **Ancillary-state retention (#124).** The ops log now rotates by size (it
  became a load-bearing read surface in 0.13.0 and grows unbounded on long-lived
  hosts), and orphaned `.prov.json` provenance sidecars are pruned in the
  retention pass.
- **Phase 3b directive dual-write (#125).** Directive-creating commands now
  mirror a first-class `directives/<id>.json` LWW record, plus a clobber-safe,
  append-only ack/route sub-log, with a report-only `_directive_parity_check`.
  This is the durable-ack work the 0.13.0 runbook called out as the thing that
  resolves `ack_drift` (see below).

### New health-record fields a reconcile now emits

After 0.14.0 is deployed and a host runs reconcile, its health record carries
more than the 0.13.0 `event_parity` block:

- **`event_parity`** now additionally carries `tasks_total`, `tasks_with_events`,
  and `folds_complete` — coverage + completeness, alongside the existing
  `drift` / `ack_drift` counts.
- **`event_dual_write`** block — including `append_failures_recent`, so a host
  silently failing to append shards becomes visible instead of invisible.
- **`directive_parity`** block — the report-only directive fold-vs-file check.

These are the **flip-readiness signals.** In particular, `directive_parity` plus
the durable directive acks from Phase 3b (#125) are what let the read-cutover's
`ack_drift` clear once 0.14.0 is deployed fleet-wide: the 0.13.0 runbook
(section 5, step 3) flagged `ack_drift` as expected-nonzero and flip-blocking
*until ack durability landed in Phase 3b* — that's this release. Watch `drift`,
and now expect `ack_drift` to be able to reach zero too once the whole fleet is
on 0.14.0.

### Post-deploy check

In addition to the 0.13.0 checks (section 4 — `events/tasks/` filling,
`event_parity` appearing), one new thing should appear on the bus once a 0.14.0
host does directive work: a `directives/<id>.json` tree, as the Phase 3b
dual-write goes live.

```bash
# default root is /coordination; substitute FULCRA_COORD_REMOTE_ROOT if overridden
<file-capable-cli> file list /coordination/directives/
```

Today that path is empty (no host dual-writes directives). Seeing records there
is the Phase 3b dual-write proving it works end to end.

---

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
fulcra-coord doctor       # must report CLI reachable and File commands OK
```

If it still reports 0.12.0, the reinstall didn't replace the on-PATH binary —
check that `~/.local/bin` is ahead of any other `fulcra-coord` on `PATH`.

### Client bump: fulcra-api ≥ 0.1.33

The fleet's **fulcra-api client** is what fulcra-coord shells out to for the
brokerless bus file transport (`file list/download/upload/stat/delete`). Bump it
to the latest released version:

```bash
uv tool install 'fulcra-api==0.1.33' --force
uv tool list | grep fulcra-api   # confirm the bump took
fulcra-coord doctor              # confirm the invoked client is file-capable
```

**Gotcha (real, just hit):** On some hosts, `fulcra-api` was installed from a
now-deleted local source path (e.g. `/private/tmp/fulcra-api-python`), so `uv tool
upgrade fulcra-api` FAILS with "Distribution not found at: file://…". Use the
explicit version form (`uv tool install 'fulcra-api==0.1.33' --force`) above to
move such a host onto the released build. If a host intentionally runs a LOCAL/dev
build of fulcra-api (e.g. for unreleased features), point it at that source
instead — don't blindly force it to PyPI.

`uv tool install` updates the uv-tool shim; it does **not** override an explicit
`FULCRA_CLI_COMMAND`. If `fulcra-coord doctor` still reports a local/dev command,
keep that only when it intentionally points at a current file-capable build.
Otherwise update/unset `FULCRA_CLI_COMMAND` in the host shell/launchd environment
and reinstall listener/heartbeat jobs so background processes inherit the same
client that `doctor` reports.

The `fulcra-api` `file` command surface is unchanged 0.1.32→0.1.33 (identical
command set; the delta is internal), so this bump is safe and loses nothing — it's
stay-on-latest-client hygiene. The `File commands: OK` check from the verification
section below still applies.

## 4. Post-deploy verification

Once at least one upgraded (0.13.0) host has done real task mutations
(`start` / `update` / `done` / `block`), two things should appear that are absent
today:

First, confirm which Fulcra CLI command has the `file` command group on this
host:

```bash
fulcra-coord doctor
```

Use the file-capable command reported by `doctor` for the file checks below. On
some hosts that is `fulcra`; on others it may be a configured command such as
`uv run --project /Users/<you>/Developer/fulcra-api-python fulcra`. Do not assume
the public `fulcra-api` command has file support unless `doctor` explicitly
reports File commands OK for it.

**(a) The event tree starts filling.** An `events/tasks/<id>/` subtree appears
under the coordination root and shards accumulate, one immutable file per
mutation:

```bash
# default root is /coordination; substitute FULCRA_COORD_REMOTE_ROOT if overridden
<file-capable-fulcra-cli> file list /coordination/events/tasks/
# then drill into a specific task to see the shards pile up
<file-capable-fulcra-cli> file list /coordination/events/tasks/TASK-<id>/
```

Today that path is empty (no host dual-writes). Seeing shards is the dual-write
proving it works end to end.

**(b) The reconcile parity record appears.** On an upgraded host, run reconcile,
then read that host's health record:

```bash
fulcra-coord reconcile
<file-capable-fulcra-cli> file download /coordination/health/<host>.json   # then read it
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

---

## 0.15.0 — what's new since 0.14.0

0.15.0 is again **purely additive** and changes nothing about how reads resolve
(the read-cutover flip remains a separate, operator-gated decision). It ships
two addressing upgrades and the **coordination loops** substrate — the
request→response layer that makes every cross-agent ask a tracked, bus-closed
loop.

**Upgrade procedure unchanged:** per host, `git pull && uv tool install
--reinstall --force .` from `packages/fulcra-coord`, then `fulcra-coord
--version` should report `0.15.0`.

### Addressing (#128, #129)

- **`@<role>` audiences (#128).** A directive may be addressed to a logical
  role (e.g. `@reviewer`) instead of a frozen agent id. It resolves at READ
  time against each agent's declared roles (`connect --role <name>`), with
  multi-holder fan-out — every live holder sees it; a stale id can no longer
  silently strand a message. Old buses/agents are unaffected until roles are
  declared.
- **Config-driven review routing (#129).** The hard-coded fleet reviewer ids
  are gone from core. Reviewer preference now comes from
  `${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/review-routing.json` (or
  `FULCRA_COORD_REVIEW_SEED`), defaulting to a purely capability-driven pool
  (`connect --can-review`). Install your fleet's policy file per
  `review-routing.example.json`.

### Coordination loops (#130, #135, #137)

- **Loops**: directives gain `kind` (review / dispatch / idea, plus legacy
  `tell`) with declared lifecycles. A loop that expects a response stays OPEN
  until a response lands **on the bus** — `fulcra-coord respond <loop-id>
  --outcome … --evidence …` (or `review-done` for reviews). Outcomes live as
  append-only response shards under the directive's `responses/` sub-log;
  snapshots are caches. A forge comment never closes a loop.
- **Detection**: `status` now warns on overdue loops and loops awaiting you;
  each reconcile records a `loop_health` block ({open_loops, overdue,
  awaiting_me}) in the host health record; the listener notification appends
  the overdue count.
- **Self-healing listener**: `connect` (every SessionStart) re-arms a missing
  notify-inbox job idempotently and verifies it is actually loaded, not just
  written. Opt out with `FULCRA_COORD_ENSURE_LISTENER=0`.

### Post-deploy fleet transition (operator-relayed)

Once all hosts report 0.15.0: reviewers run `connect --role reviewer
--can-review`; the maintainer pins `--role coord-maintainer`; routing docs
flip from frozen reviewer ids to `@reviewer`. Until then everything behaves
exactly as 0.14.0.

---

## 0.15.1 — what's new since 0.15.0

Patch release: the two bugs found by the 0.15.0 live validation, plus loops
phase 2. Purely additive; upgrade procedure unchanged (`git pull && uv tool
install --reinstall --force .` from `packages/fulcra-coord`; verify
`fulcra-coord --version` reports `0.15.1`).

- **Claiming a review no longer breaks it (#140).** A status transition was
  dropping the `kind:review` routing marker, so a reviewer who CLAIMED a review
  could not deliver the verdict via `review-done <artifact>` (forced `--to`,
  loop never closed). Fixed; claim freely.
- **Reconcile rides out backend throttling (#141).** View uploads retry once
  with jitter when the deadline allows (`FULCRA_COORD_UPLOAD_RETRY=0`
  disables); failed uploads now record the transport stderr to the ops log.
- **Loops phase 2 (#139).** `fulcra-coord board` (awaiting-me / awaiting-others
  with ⚠ overdue + ◈ out-of-band / in-flight / ideas), a loops line in the
  digest, and `fulcra-coord forge-mirror --once` — the one sanctioned forge
  poller, mirroring verdict-shaped GitHub signals as marked evidence that can
  never close a loop.
