# Changelog

All notable changes to **fulcra-coord** — the shared agent-coordination layer
that lets independent agents (Claude Code, Codex, OpenClaw, ChatGPT, CI)
coordinate durable tasks over Fulcra Files as a bus, with no shared memory or
direct calls.

Format loosely follows [Keep a Changelog](https://keepachangelog.com/); released
versions are sourced from `fulcra_coord/__init__.py::__version__`.

---

## [Unreleased]

### Installer-baked role declarations

- `install-claude-code` can now bake `--can-review` and repeated `--role ROLE`
  declarations into the installed SessionStart `connect` hook.
- `install-codex` can now bake repeated `--role ROLE` declarations into the
  installed SessionStart hook while preserving Codex's default review
  capability. The hook materializes connect flags as a bash array so role names
  stay shell-safe.
- Baked declarations are persisted beside the managed hook config and reused on
  later reinstall/self-heal rewrites, so `ensure-codex-watch` does not silently
  erase roles installed earlier.

### Scheduled agent reminders

- Added `fulcra-coord remind ASSIGNEE WHEN TITLE`, a bus-native scheduled
  directive. It stores `WHEN` as `not_before`, rejects unparseable reminder
  times, and uses the existing directive writer/dual-write path so reminders
  behave like normal inbox work once due.
- Agent inbox routing now respects future `not_before` fields. Future scheduled
  directives stay out of `inbox`, listener notifications, and
  `index.counts.inbox` until the gate passes; malformed/empty gates continue to
  behave as immediately visible.
- During a mixed-version rollout, pre-196 hosts may still surface scheduled
  reminders immediately until they self-update. That failure mode is benign
  early visibility, not missed notification.

### Listener ticks keep reviewer routing presence fresh

- `notify-inbox --agent X` now refreshes `X`'s presence timestamp while
  preserving workstreams, summary, session, and capabilities. This closes the
  gap where a durable listener could keep seeing direct inbox work for a
  reviewer, while `request-review` still classified that reviewer as
  below-floor because no full agent session had refreshed presence recently.

### Message-class lifecycle: delivered tells/FYIs/echoes finally reach a terminal state

The operational symptom (2026-06-11): message-class directives — tells, FYIs,
acks, review-verdict echoes, anything with `expects_response` falsy — never
reached a terminal state. Nobody marks a delivered message done, the archive
sweep only collects done/abandoned tasks, so 211 of ~480 hot tasks sat
`status=proposed` forever, monotonically bloating every listing the platform
gateway serves under its ~15s limit. Two fixes:

- **Age-based auto-close in the retention pass** (`_close_stale_messages`,
  sibling of broadcast expiry): a `proposed` directive with a concrete
  assignee, a tell-shaped loop kind (or none), and `expects_response` falsy is
  transitioned proposed→done once `created_at` is ≥
  `FULCRA_COORD_MESSAGE_TTL_DAYS` (default 7) days old, with evidence
  `"delivered message auto-closed after N days (message-class TTL)"` through
  the normal writepipe (views/events stay consistent). Same budget/cap/
  per-item-isolation discipline as the sibling sweeps; the existing
  cold-archive then clears the closed message on a later pass (recoverable via
  `restore`). **Deliberately NEVER auto-closed** (test-pinned): anything with
  `expects_response` truthy — the closed-loop guarantee: a loop stays open
  until a bus-native response, period; review/dispatch/question/signoff (and
  unknown) loop kinds; broadcasts (their own 14d expiry); `kind:idea` backlog
  items; self-owned work tasks (no assignee); non-proposed statuses; undatable
  `created_at` (parse-don't-compare — keep what we can't date).
- **`proposed → done` is now a legal transition** (with the existing
  `--evidence` + verification-level requirements intact): a delivered
  message's consumer closing the echo is the NORMAL case, and the old
  two-write dance (update→active, then done) over a high-latency transport
  silently discouraged cleanup. proposed→active/waiting/abandoned unchanged;
  proposed→blocked stays illegal.

### Quick wins from the 2026-06-11 live finds: unreplayable repair markers self-clear (and stop being minted), and `start` refuses task-id-shaped titles

Two operator-reported bugs from the live bus, fixed at both the symptom and
the source:

- **No-body op markers self-clear.** 15 zombie repair markers (ops.log reason
  "no cached body to replay") survived every reconcile until manual deletion:
  a failed/unverified marker whose task has no locally cached body could never
  be repaired (nothing to replay) and never cleared (each failure fails the
  tick, which preserves all markers). `cmd_reconcile`'s body-repair loop now
  resolves these by looking at the remote instead of failing blind: remote
  body **readable** → the write evidently landed by another path, marker
  cleared; remote **confirmed absent or tombstoned** (the
  `io._confirmed_absent` idiom from the tombstone work) → nothing can ever
  replay it — pure debt, no asset — cleared with ops-log reason "no cached
  body and remote absent — unreplayable marker cleared"; remote state
  **unknown** (transport failure) → marker kept, failing toward retry, never
  toward forgetting an unproven write.
- **…and stop being minted.** The source was `writepipe._write_task_and_views`
  caching the task body only *after* a successful upload — a failed upload
  returned early with a failed/needs_reconcile marker and no cached body.
  Callers that create their task straight through the pipeline with no
  pre-caching of their own (request-review's escalated-to-human path was the
  live source) minted an unreplayable marker on every upload failure. The body
  is now cached *before* the upload attempt, so every marker is replayable by
  construction; the reconcile branch above drains the pre-fix zombies.
- **`start` stops eating task ids.** `start TASK-…` (the operator meant to
  *claim* an existing task) created junk tasks titled after task ids — 6 found
  on the live bus. The previous non-blocking "you probably meant to claim"
  hint demonstrably didn't prevent them, so an id-shaped TITLE
  (`^TASK-\d{8}-`) is now refused outright: exit 1, nothing created locally or
  remotely, with a pointer at `fulcra-coord update <id> --status active`.
  Genuine titles are unaffected.

### Product-calls perf wave: facade summaries, one-process session start, zero-reader views retired

Three operator-approved cuts to remote-op fan-out (every remote op is one
`fulcra-api` subprocess spawn, ~1.3s median vs 1-16s natural latency):

- **ChatGPT facade reads summaries, not full bodies.** Both facade read
  surfaces paid `cli._load_all_tasks` — index + search-index + next + one
  body fetch per task, N+3 spawns (~480 at current bus size) — on every
  call. `GET /coordination/status` is now ONE `views/summaries.json`
  download (build_index is summary-complete by the existing equivalence
  invariant); `POST /coordination/report`'s find-or-create matches
  owner_agent + the session tag on summaries and fetches only the matched
  task's body: 1 + at most 1 spawns. The field-by-field check found no
  summary gaps. Call-count pins added to the facade tests.

- **`briefing` subcommand + session-start hooks consolidated.** The
  SessionStart hooks (Claude Code + the Codex twin) ran identity + status +
  inbox + needs-me as 4 CLI processes, each re-downloading the summaries
  view — and in stale-view degraded mode each could re-run the whole
  direct-listing fallback (up to 4 repair-shaped bursts per session start).
  `fulcra-coord briefing --format json` folds all four sections from ONE
  summaries load (pinned == 1); both hooks now make a single foreground CLI
  call, so degraded mode falls back at most once. Hook fail-safe/identity
  contracts (exit-0 on any error, no pinned `--agent`, declared-id
  precedence, section order) re-verified end-to-end through bash.

- **Zero-reader views retired.** `agents/<id>.json` (one per owner/toucher
  identity) and `views/inbox/<slug>.json` (one per open-directive assignee)
  were rebuilt and uploaded on EVERY write/reconcile — ~A+I ≈ 35+ uploads
  per pass at current fleet size, scaling with fleet growth — and read by
  nothing (re-verified by grep: `agents`/`resume` fold the summaries
  aggregate client-side; `inbox` recomputes from the task set; the
  per-assignee counts live on as `index.counts.inbox`).
  `views.build_all_views` no longer emits them; `views.build_agent_view`,
  `remote.agent_remote_path`, and writepipe's `agents/` name mapping are
  removed as orphans. Existing remote files under those prefixes are
  deliberately NOT deleted — bus-state cleanup is deferred pending a Fulcra
  service review; they are inert and simply never refresh again. The
  summaries freshness beacon / stale-view guard are unaffected (they key
  off `generated_at`, which neither retired view carried). Per-write
  fan-out at the test topology: 9 → 8 first-write uploads, 4 → 3
  changed-view uploads (test_view_skip pins updated with the why); on the
  live bus a full view pass drops from ~55 to ~20 files.

---

## [0.15.5] — 2026-06-12

**Listener hot-path release.** `notify-inbox` no longer pays the optional
overdue-loop directive scan by default, so launchd listener ticks can write the
inbox surface, emit notifications, wake configured agents, run self-update, and
exit without being pinned behind a large directive download fan-out. When the
summaries view is stale, scheduled listener ticks now also serve that stale view
for the tick instead of winning the direct-listing fallback claim and rebuilding
from every task body. Hosts that want the old notification suffix or
repair-shaped listener fallback can opt back in with
`FULCRA_COORD_NOTIFY_OVERDUE_SUFFIX=1` or
`FULCRA_COORD_NOTIFY_STALE_SUMMARY_FALLBACK=1`.

Follow-up hardening: Codex now has the same installer-owned wake path as Claude
Code. `install-codex --with-wake` and `ensure-codex-watch --with-wake` seed a
reviewable `wake.json` entry that lets a pending inbox spawn a headless
`codex exec` run; without that entry, the listener remains notify/surface-only.
Listener launchd logs are now per-agent (`listener-<agent-slug>.err.log`), and
each tick emits a compact breadcrumb with agent, pending count, new count,
surface path, and wake result so “armed” can be audited from logs.
`ensure-codex-watch` now reloads an already-loaded launchd listener after
rewriting its plist; previously `launchctl load -w` left the old in-memory job
running, so cadence/log-path changes on disk did not take effect.
Scheduled listener ticks now also emit a throttled operator alert when the
summaries view is stale past `FULCRA_COORD_NOTIFY_STALE_ALERT_MIN` (default
60m). This keeps the bounded stale-view mode from failing silently during
chronic view outages; alert cadence is controlled by
`FULCRA_COORD_NOTIFY_STALE_ALERT_INTERVAL_H` (default 6h).
Codex SessionStart now passes its thread/session id into `ensure-codex-watch`,
which installs a managed Codex heartbeat automation for that thread. This fixes
the missing app-layer listener: hooks/listener/wake are no longer expected to
stand in for the Codex thread automation that actually wakes an open
conversation to poll the bus. The managed heartbeat defaults to 15 minutes and
`ensure-codex-watch` prints the automation id/thread/cadence when it writes or
updates it. Headless `codex exec` wakes are marked with
`FULCRA_COORD_CODEX_WAKE=1`; if they run SessionStart hooks, they refresh
hooks/listeners but skip thread automation retargeting so they cannot steal the
heartbeat from the live app thread.

## [0.15.4] — 2026-06-11

**Reliability release.** The through-line: the transport stops lying to the
code above it. Read failures no longer read as absence anywhere they could
destroy or skip work (write path, roles/presence, retention, directives), and
the platform's soft-delete tombstones are now recognized for what they are —
deliberate deletions — by the repair loop, the archive gates, and restore.
On top of that honesty layer: writes upload only the views that actually
changed (~10x cut in per-write bus fan-out, with reconcile keeping its role
as the drift repair), transient transport failures retry instead of failing
the operation, the repair queue rotates failing markers out of the head so
one poison marker can't starve the rest, and a per-host breaker stops the
direct-listing fallback stampede that could saturate a host's API gateway
indefinitely. The loop-2 perf wave (six mechanical double-read/over-download
eliminations) and the orphan cleanup ride along.

**Upgrade note — hosts on ≤0.15.3 should move promptly:** older versions
re-upload every view on every write, generating heavy bus write amplification
that this release eliminates. With self-update (0.15.3+) the fleet picks this
up from the announced manifest; opted-out hosts need the manual pull.

### Fallback stampede breaker: one direct-listing fallback per host at a time

Live find (2026-06-11, the self-sustaining stampede): when the bus views go
stale/broken, `_load_task_summaries`' stale-view guard sends every reader to
the direct-listing fallback — one `tasks/` listing plus ~450 per-task
stat/body fetches at current bus size. That loader runs in EVERY listener
tick, and the operator's Mac runs eight listeners: every tick of every
listener fell back simultaneously, the host saturated the API gateway with
its own concurrent subprocesses (observed 15-18 concurrent `fulcra-api`
calls around the clock; a single notify-inbox tick running 40+ minutes), all
calls queued and timed out at the gateway, the views could never repair, and
the stampede sustained itself indefinitely. The operator misread the result
as a backend 504 outage — twice.

Fix — a per-host throttle on the fallback (`io._claim_fallback_throttle`):

- **One claim per host at a time.** Before entering the fallback, a caller
  must claim a LOCAL marker (`<cache>/roots/<slug>/fallback-throttle.json` —
  the XDG cache is per-host/per-OS-user, so all of a host's listeners share
  it; scoped per remote root so one bus's throttle never gates another).
  The claim reuses wake.py's pidfile idiom: `O_CREAT|O_EXCL` as the
  inter-process mutex, stale-by-mtime takeover for crashed holders.
- **Throttled callers serve the stale view** (with a warn naming the rate
  limit and the holder's age) and make ZERO listing/body calls — clearly the
  lesser evil vs joining the stampede; the one running fallback repairs the
  cache for everyone.
- **Completion releases — success or failure.** The window
  (`FULCRA_COORD_FALLBACK_WINDOW_MINUTES`, default 10; `<= 0` disables) only
  guards concurrency, never rate across time; takeover handles crashes.
- **The reconcile path bypasses the throttle**
  (`bypass_fallback_throttle=True` from
  `_reconcile_rebuild_source_preserving_acks`): reconcile's job is exactly
  to repair the views, so it must never be locked out by listener fallbacks.

### Archive gates: a tombstoned cold copy no longer counts as "already archived" (the F7-undoing hazard)

Found during the tombstone-absence work — the same stat-dishonesty class,
opposite direction. The platform delete is SOFT, so after `restore` deletes
the archive copy, `stat` on the archive path still answers from version
history. `retention._archive_task`'s gates read that stat as "cold copy
present": the next retention pass on a re-aged restored task (reachable since
the restore-sticks fix ages from `restored_at`) skipped the fresh archive
upload, its stat-based post-upload verify passed vacuously, and it deleted
the hot copy — the task body GONE from the hot path with only a tombstone in
the archive, silently undoing the operator's restore. Fix (the no-loss
ordering upload → verify → delete-hot is unchanged; only the gates got
honest):

- **`_cold_copy_state`** — tombstone-aware presence probe for archive-side
  paths, the `io._confirmed_absent` signature applied to the presence
  question: readable JSON body ⇒ present; not-found-class download failure on
  a reachable bus ⇒ tombstone ⇒ ABSENT (do the fresh upload); transient or
  unknown failure ⇒ UNKNOWN ⇒ the task is deferred this pass, hot copy kept
  (fail toward keeping the hot copy, never toward deleting it).
- **Post-upload verify is now a readable-body check** (`download_json`, never
  a bare stat): a lying/failed upload over a tombstone can no longer
  stat-verify and unlock the hot delete. One extra download per archive move
  — archival is a daily background pass.
- **Index-shard gate same fix**: `restore` soft-deletes the shard too, so the
  old stat gate skipped rewriting it on re-archive, leaving the task
  invisible to `search --archived` and unrestorable. A readable shard is now
  required; rewriting over a transient read failure is harmless (same id,
  same path, fresh `archived_at`).
- **`cmd_restore`'s mirror verify**: the hot path is tombstoned from the
  original archive move, so its stat-based post-upload verify was vacuous —
  an upload that claimed success without landing would have passed verify and
  the archive-body delete right after would have destroyed the ONLY readable
  copy. The hot copy must now download readable before any cold-side delete.

### Tombstone-aware absence: soft-deleted remote files are now confirmably absent

Live find (2026-06-11, the ~12 forever-blocked repair markers): the Fulcra
Files platform DELETE is a SOFT delete — a deleted file keeps version history
that `fulcra file stat` still reports, while `download` fails
deterministically with a not-found-class error. Every absence check built on
"stat is None ⇒ maybe absent" (`io._confirmed_absent`, the repair loop's C2
guard) therefore read a tombstoned path — exactly what the archive move and
every retention prune produce — as "exists but unreadable": absence was never
confirmable, and repairs/writes against tombstoned paths re-failed every
pass, forever. Three-part fix:

- **`_parse_stat` honesty (store).** It returned a truthy `{"raw": text}`
  fallback for ANY non-empty stdout, so message-shaped output (a
  "not found"-style line with rc 0, a bare JSON scalar) read as "the file
  exists". Output carrying none of the expected stat fields (`_STAT_FIELDS`:
  the strong identity keys, weak indicators, and path echoes that
  `stat_changed` and the parser actually use) now parses to None. No caller
  consumed the raw-only fallback (the only `"raw"` consumer is
  `stat_changed`'s last-resort comparison, reachable only via the removed
  fallback itself).
- **Tombstone signature in `io._confirmed_absent`.** A visible stat now costs
  one fresh download probe: readable ⇒ present; failing with a POSITIVE
  not-found-class error (the new `store._is_not_found_failure` — deliberately
  narrower than "non-transient", so silent/unknown failures stay fail-safe)
  while the bus probes reachable ⇒ tombstone, absence CONFIRMED; transient or
  unknown failure ⇒ unconfirmable, exactly as before. `download` gained the
  `last_download_error` observable (the exact counterpart of
  `last_upload_error`, same documented semantics) so the absence layer can
  read the failure class. This also unblocks the retention orphan-shard GC,
  which requires positive absence of the (soft-deleted) hot task file.
- **Repair loop: tombstone ⇒ resolve the marker, never resurrect.** A
  tombstoned task path means someone deliberately deleted the task, so the
  repair must NOT re-upload the cached body (resurrection-by-repair — the
  F7-adjacent hazard class). Instead it consults the archive cold-index:
  archived ⇒ the marker is obsolete (the truth lives in the archive; reason
  `tombstone: archived, marker cleared`); not archived ⇒ operator intent is
  ambiguous, and clearing with a pointed ops-log trail (reason `tombstone:
  not in archive, marker cleared without re-upload`; the body remains
  recoverable via the platform's version history / `fulcra file restore`)
  beats silently resurrecting a deleted task. Both paths evict the local
  cached copy (same rationale as `_archive_task`: the cache-seeded loader
  would rebuild the dead id into the views) and log a distinct
  `task_body_repair_tombstone` ops-log entry per task. Transiently-unreadable
  live bodies keep the existing marker-kept/backoff behavior.

The test fake (`fake_fulcra_backend.py`) now models the soft delete via a
`<path>.tombstone` sibling (stat answers with prior-version metadata,
download 404s, list hides it); its default `delete` stays a hard unlink so
every pre-tombstone pin keeps its semantics.

### Reconcile: repair-queue starvation fix — failing markers rotate out of the head

Live find (2026-06-11, post-recovery): the task-body-repair loop in
`cmd_reconcile` iterated op markers in `cache.list_op_markers()` glob order.
~12 markers failed deterministically every pass, each re-failure costing
30–60s of remote ops (download + stat probe + upload, with transient
retries) — and they sorted at the HEAD of the order, so every pass (even a
900s one) burned its whole budget re-failing the same head and deferred the
~60 healthy markers behind it at the budget floor. A 900s pass repaired ~1 of
72; the queue could never drain past the failing head. Two-part fix:

- **Per-marker attempt bookkeeping + backoff.** A FAILED repair attempt now
  stamps the marker (`repair_attempts`, `repair_last_attempt_at` — house ISO
  via `timeutil.now_iso`). At loop start markers are partitioned:
  never-attempted ones run FIRST (first claim on budget), previously-failed
  ones whose backoff expired run after on leftover budget, and markers still
  inside their `min(2**attempts, 32)`-minute window (`
  _REPAIR_BACKOFF_CAP_MINUTES`, patchable) are SKIPPED for the pass — marker
  KEPT (skip is debt, not success) and retried after the window. Stamps are
  parsed (`views._parse_dt`), never compared lexically; an unparseable stamp
  fails toward retrying. The #172 budget-floor deferral semantics are
  unchanged.
- **Per-task failure reasons.** The aggregate `task_body_repair_failed`
  ops-log entry listed only ids — tonight's diagnosis had to guess. Each
  failure site now records a short reason (`upload failed: <stderr tail>`,
  `remote stat exists but fresh download unreadable`, `absence unconfirmable
  (stat probe failed)`, `unsafe merge: … cached side newer`, `no cached body
  to replay`), the ops-log detail carries a `{task_id: reason}` mapping
  (reasons truncated ~120 chars), and the first 3 reasons are warned inline
  so an operator tail shows WHY.

### Perf, loop 2: six mechanical double-read/over-download eliminations

Every remote op is one `fulcra file` subprocess (~1.3s median against a
backend with 1–16s natural latency); this pass removes spawns that paid for
the same bytes twice or downloaded bodies nobody read. No behavior changes —
only fewer remote ops, each pinned by a counting-fake test in
`tests/test_perf_call_counts.py` (E5–E10):

- **Role fold (health tick / board / `roles`):** `list_roles` downloaded
  EVERY `.json` under `roles/` (lease shards + escalation markers included)
  then discarded the non-top-level ones, and `read_leases` re-listed and
  re-downloaded each role's shards. One partitioned listing
  (`role_ops.load_roles_with_leases`) now serves registry + leases: with R
  roles, L lease shards, E escalation markers, each render drops from 1+R
  listings and R+2L+E downloads to 1 listing and R+L downloads (pinned: 3→1
  listings, 9→5 downloads on the 2-role/3-lease/1-marker fixture). The #171
  F4 discipline is preserved — a listed-but-unreadable lease shard still
  surfaces as `READ_ERROR`, never as the `[]` that folds to VACANT.
- **Listener tick (`notify-inbox`):** the inbox fold and the needs-me pass
  each loaded `views/summaries.json`; the tick now loads once and threads it
  through (2→1 downloads per tick per agent — and in stale-guard mode the
  saved load was a full ~one-spawn-per-task direct-listing fallback re-run).
- **Evidence probe (`evidence_ids_for`, on board/digest/reconcile):** only
  listing-NONEMPTINESS is consumed, so the probe is now a paths-only
  `list_files` instead of `list_json` — 1 list + 0 downloads per candidate
  (was 1 list + one download per shard).
- **Directive dual-write:** the `if responses:` probe and `fold_loop` each
  swept the responses sub-log; the probe's read is now threaded into the fold
  (`fold_loop(responses=...)`) — 2→1 lists (+K shard downloads saved) per
  directive-creating write.
- **Digest:** `cmd_digest` and `_assess_fleet` each loaded the summaries
  aggregate (the latter only for `len()`); threaded through — 2→1 downloads
  per digest.
- **Reconcile health record:** re-downloaded `retention/last-run.json` although
  the retention pass's throttle claim had just read (or written) it; the claim
  now returns the observed marker and the tick reuses it — 3→2 downloads on a
  running-retention tick, 2→1 on the throttled steady state (~every tick).

### Retention/directives wave: destructive paths stop trusting failed reads; the GC that never ran now runs

Seven fixes from the 2026-06-10/11 blind audit + live operation, sharing two
themes: (a) the READ_ERROR/`_confirmed_absent` discipline extended to the
remaining destructive call sites — absence needs POSITIVE evidence before
anything is deleted or overwritten; (b) long loops compose with reconcile's
deadline instead of overrunning it.

- **Event-log orphan GC requires confirmed absence (F6):** the orphan branch
  of `retention._prune_event_log` treated a stat-`None` — which the transport
  also returns on a FAILED read — as "hot file confirmed gone" and deleted the
  task's whole event-shard tree; compound with a partial task load and a LIVE
  task's fold source was destroyed. The branch now requires
  `io._confirmed_absent` (stat miss + reachable bus); unconfirmable absence
  defers the task to the next pass.
- **`restore` now sticks (F7):** `cmd_restore` left the archive body in place
  and the task terminal+aged, so the next daily retention pass re-archived it
  within ~24h — silently undoing the operator's restore. Restore now deletes
  the cold copy after verifying the hot body landed (the `_archive_task`
  no-loss ordering, reversed; a failed cold delete keeps the index shard and
  errors so a retry finishes the move instead of letting a stale archive body
  resurrect later), stamps `restored_at` on the hot body, and
  `views.is_archivable_task` ages from max(done_at, restored_at) — a restore
  opens a full fresh retention window.
- **Directive snapshot refresh can no longer regress (F9):** `dual_write`
  rebuilt the LWW snapshot from three best-effort sub-log reads that returned
  `[]` on failure, so a re-mirror during a listing blip shrank `acked_by`
  below the durable union and re-opened closed loops on every board/digest
  until the next good fold. The reads now report failure distinctly
  (`directives._read_sublog_shards`); on any failure the refresh
  merge-preserves the previous snapshot's acks/terminal state (the upload can
  only ADD), and skips entirely (ops-logged `directive_snapshot_skipped`)
  when there is nothing safe to merge from.
- **`review-done` failure path could never be audited (NameError):**
  `routing_ops` called `ops_log.log_op` without importing `ops_log`; the
  NameError was swallowed by the guarding try/except, so the
  `response_write_failed` entry was never written. Import added (the
  writepipe style) and the entry pinned by test.
- **Continuity-checkpoint GC never ran in production (listing contract):**
  `_walk_continuity_checkpoint_dirs` descended trailing-slash directory
  entries, but the live `fulcra file list` returns RECURSIVE file listings
  with no directory entries — the walker found zero children and the
  unbounded `checkpoints/` growth was never pruned. Rewritten to partition
  ONE recursive listing by path segments (one list call instead of
  O(directories); tolerant of backends that do emit dir entries), with the
  mock tests re-encoded to the live contract and a fake-backend-shaped pin.
- **Role escalation markers are now pruned:** `roles/<name>/escalations/
  <YYYY-MM-DD>.json` (minted daily per vacant role) accumulated forever and
  every roles listing paid for the pile. The marker-prune pass now sweeps
  them on the existing marker-retention window with the same
  parse-don't-compare + fail-toward-keeping discipline (role records and
  leases can never match the prune shape).
- **Reconcile's body-repair loop is deadline-gated (live find):** the loop
  ran every queued repair regardless of budget — with the #167 transient
  retry (≤61s/op worst case) a 42-item backlog ran 40+ minutes and overlapped
  the next cron tick. It now checks the same budget floor `_run_retention`
  uses between items, defers the remainder (markers kept, ops-logged
  `task_body_repair_deferred`), and the remainder drains next tick.

### Roles and presence: read failures no longer read as vacancy or absence

**The same class, second wave (2026-06-11 adversarial audit, F4/F5/F8):** the
write-path fix below closed the absence-vs-read-failure conflation for task
writes; the roles and presence layers had three more instances of it, each
ending in a durable wrong outcome — a false P1 on a human's plate, a live
reviewer rerouted away from, an agent's declared record wiped. Same C1
discipline throughout: a failed read is disambiguated (stat probe +
`probe_reachable`, spent only on failure paths) and surfaced as a sentinel;
nothing escalates, reroutes, or rewrites on a guess.

- **`role_ops.read_leases` (F4):** returned `[]` on ANY failure, and `[]`
  folds to VACANT in `roles.role_status` with `vacant_since` = the role's
  (old) `created_at` — instantly past any SLA, so ONE failed lease listing
  pushed a false "Role VACANT past SLA" P1 directive onto the maintainer's
  plate (the daily marker capped it at one/day, but each is a durable false
  alarm a human has to read and dismiss). `read_leases` now returns the
  `READ_ERROR` sentinel (the `read_role` idiom) when the listing failed, a
  listed shard wouldn't download, or an empty listing can't be confirmed
  against a reachable bus; `role_status` grew an explicit `unknown` outcome
  (never vacant, no SLA clock) for unknowable inputs; the vacancy escalation
  is SKIPPED with a logged reason; and every read surface (`roles`, the
  board's Roles section, role health) renders "leases UNREADABLE" instead of
  VACANT. The exclusive-policy check inside `claim_role` treats an unreadable
  sub-log as advisory-skip — the claim itself still lands (per-agent shards
  are clobber-free).
- **`presence._reconcile_presence` / `_upsert_presence_aggregate` (F5):**
  `remote.list_json`'s per-item isolation silently DROPS a record whose
  individual download fails, and both rebuilds uploaded the SURVIVORS as the
  authoritative aggregate — a live reviewer whose one record 504'd vanished
  from the roster, and the truncated roster threaded into the review-route
  sweep and role health that same tick ("no reviewer live" escalated to the
  human while the reviewer was up — lived incident). The new opt-in
  `remote.list_json_checked` variant exposes completeness (existing
  `list_json` callers keep their contract untouched); a PARTIAL rebuild now
  never uploads — the previous full aggregate stays, is handed to the tick's
  consumers (stale-but-full beats fresh-but-truncated), and the boundary case
  (per-agent read partial AND the previous aggregate unreadable) fails toward
  NO-ACTION: the review sweep is skipped outright and role health reports
  every role unknown. The staleness-guarded roster loader likewise no longer
  swaps a full stale aggregate for a partial per-agent listing.
- **`presence.cmd_connect` / `cmd_workstream` / the capability RMW (F8):** a
  failed read of the agent's OWN presence record was treated as "never
  connected", and the subsequent whole-record write wiped
  capabilities/workstreams/summary/session (the C4 comment admitted the bare-
  connect exposure; `workstream add` could wipe everything). `_load_own_presence`
  now disambiguates per the C1+probe idiom; on a failed read, connect SKIPS
  the presence write with a loud warning (a missed heartbeat heals on the
  next connect; a wiped record needs an operator), `workstream set/add/clear`
  aborts with a clear error, and `add_capabilities`/`remove_capability`
  refuse the rewrite. A genuinely-first connect (probe-confirmed absent on a
  reachable bus) still writes the full fresh record, and an explicit
  `--clear-roles` still rebuilds — the operator sanctioned it.

### Write path: transport read failures are no longer treated as absence

**The class (2026-06-11 adversarial audit):** the transport primitives
(`fulcra-coord-files` `store.stat`/`download`/`list_files`) intentionally
return `None`/`[]` for BOTH "the remote says this doesn't exist" and "the read
failed" — and three write-path call sites acted on that `None` as if it were
proof of absence, turning one transient 504 into a destructive write. The
correct idiom already existed in one place (`role_ops.read_role`'s
`READ_ERROR` sentinel: failed download → stat probe → `probe_reachable`
before anyone may act on "absent"); this change applies the same discipline
to the write pipeline. The reachability probe is spent only on the failure
path, so the happy path costs no extra spawns.

- **`writepipe._write_task_and_views` (F1):** a pre-stat `None` was read as
  "new task, skip the merge check" — so agent A holding a stale body whose
  pre-stat 504'd would blind-LWW over agent B's just-landed `done`. Now a
  `None` pre-stat costs one body download: a readable body forces the merge;
  a missing body counts as absent only when the bus probes reachable. When
  reads are failing and absence is unconfirmed (including the fold-sourced
  branch's "file gone, nothing to merge against" hole, and a stat-visible
  body that won't download), the write FAILS instead: cached locally with a
  `failed`/`needs_reconcile` marker, so reconcile's merge-aware body-repair
  delivers the edit later by merging — never by clobbering.
- **`io._load_summaries_for_rebuild` (F2):** an unreadable summaries
  aggregate was conflated with "older bus without the aggregate" and fell
  back to `_load_all_tasks` — which itself silently degrades to LOCAL CACHE
  ONLY when the index read fails — so a cold-cache host's next write uploaded
  ALL views rebuilt from its partial cache with a fresh `generated_at`,
  silently blanking the bus's read surface (the stale-view guard can't catch
  fresh-but-truncated). Now only a CONFIRMED-absent aggregate takes the
  legacy fallback (and only when that fallback load isn't itself degraded);
  otherwise the write uploads the TASK BODY only and raises `NeedsReconcile`
  with the marker kept — the same machinery as a partial view upload.
- **`cmd_reconcile` (F3):** `_load_all_tasks` now exposes the cache-only
  degrade (`load_degraded` on its returned list), and a reconcile tick whose
  task load degraded SKIPS the view rebuild/upload phase entirely and fails
  loudly — a reconcile that can't see the bus must not rewrite the bus's
  views (the marker-driven body repairs earlier in the tick still run). A
  genuinely fresh/empty bus (index confirmed absent, bus reachable) is not
  degraded and reconciles as before.

### Write path uploads only the views that actually changed

**Why (2026-06-10 live incident):** `_write_task_and_views` rebuilds ALL views
and uploaded every one (~55 on the live bus: per-agent views for 33
identities, inboxes, workstreams, needs-attention, board) through the upload
pool on EVERY write — a fan-out that scales with fleet size while a
`tell`/`update`/`done` changes ~5 views. Under backend 504-weather (1–16s per
op), 50+ uploads per logical write meant every write ended "Task written,
views failed: [~50 names]" → NeedsReconcile, and reconcile's repair pass (the
same burst shape) couldn't drain — the repair backlog grew 67→95 across three
runs.

**What:** each view's content is fingerprinted (sha256 over the exact
serialization `upload_json` sends — `store.serialize_json`, factored out and
shared so the two can't drift — with the per-rebuild top-level
`updated_at`/`generated_at` stamps excluded, since they change on every
rebuild even when content doesn't) and the **write path's** upload is skipped
when the digest matches the fingerprint recorded at the last **confirmed**
upload. Fingerprints are local-only per-host bookkeeping
(`<cache>/view-fingerprints/`), written **only on upload success** —
deliberately NOT derived from the local view cache, which is written even for
failed uploads (so local readers see the freshest build) and therefore cannot
prove the remote is current. A failed view keeps its stale fingerprint and is
re-attempted on the next write. The `generated_at`-stamped freshness beacons
(`views/summaries.json`) always upload so the `FULCRA_COORD_VIEW_STALE_MIN`
read guard never trips on a quiet-but-healthy bus. Escape hatch:
`FULCRA_COORD_VIEW_SKIP_UNCHANGED=0` restores upload-everything.

**Division of labor — the write path skips, reconcile never does (2026-06-11
review finding):** even a success-only fingerprint proves only what *this
host* last uploaded — never the remote's *current* content, because the store
has no compare-and-swap and views are shared mutable paths another host can
overwrite after the digest was recorded. A skip on the repair path would make
such a cross-host clobber permanent: the clobbered host rebuilds identical
content, matches its own fingerprint, and skips forever. So the **write path**
keeps the skip (hot path, single-host correct, the ~10× fan-out cut) and
accepts *bounded* staleness under cross-host drift, while **reconcile**
authoritatively re-uploads every rebuilt view — never honoring the skip — so
any clobbered view is re-asserted within one reconcile cadence (~20 min), the
pre-change status quo for repair. Reconcile still records fingerprints on its
successful uploads, refreshing the write path's skip baseline so the next
write doesn't re-upload what reconcile just confirmed.

### Transport timeout defaults raised to match real latency

**Why (2026-06-11 root cause):** measured platform latency for fulcra-api calls
is 1–16s per operation (idle; worse under host CPU load). The previous defaults
(read 5s / write 15s) sat BELOW natural latency, so the client killed its own
calls: truncated listings ("unstable listings"), reads that looked like stale
views, and writes abandoned after the server had already accepted them
(observed duplicate directives) — an entire evening misdiagnosed as a backend
outage. The backend was healthy throughout.

**What:** `FULCRA_COORD_TIMEOUT_SECONDS` default 5 → 30; write timeout
`max(15, read)` → `max(60, read)`. Per-call slowness is the reconcile
deadline's job to bound, not the per-op timeout's.

### Continuity integration: checkpoint refs ride the loops and roles

**Why:** spec `docs/superpowers/specs/2026-06-10-continuity-integration-design.md`.
Two things changed: (1) the plan to move most sessions to the always-on
ArcBot box under remote control is **gated on continuity working** — a
respawned session is only useful if it can resume the dead one's work; (2)
the roles spec reserved `checkpoint_ref` for exactly this phase. Session
resume state lived in hand-rolled `.session-resume.md` files — off-bus and
invisible. Now a continuity checkpoint is a **payload ref on the
coordination primitives** (loops + roles), not a side-tree only retention
knows about.

**Storage reality (the spec's open question, resolved):** the standalone
`fulcra-continuity` CLI writes checkpoints to LOCAL paths only; the remote
`continuity/...` bus tree exists because **coord's own bridge**
(`fulcra_coord/continuity.py`) uploads the same checkpoint JSON shape there
(that's the tree the retention walker prunes). So refs are remote paths and
cross-host resume works: a handoff handed a local checkpoint file publishes
it to the bus first (the immutable archive path becomes the ref), with an
inline-payload fallback when the publish fails.

**What:**
- **`fulcra-coord handoff [--to <agent|@role>] [--checkpoint <ref|file>]
  --title ...`** — hand work to another agent WITH its resume state: an
  ordinary `kind=dispatch` expects_response loop whose payload carries
  `checkpoint_ref`. Local checkpoint files are published to the bus tree;
  opaque refs pass through verbatim (pinned: coord never parses refs). The
  recipient's `inbox` shows `checkpoint:`; claiming the task prints the ref
  + (when the optional `fulcra-continuity` CLI is installed — probed with
  `shutil.which`, invoked as a subprocess) the rendered resume brief.
- **`fulcra-coord checkpoint --role X [--ref R]`** — the role registry's
  `checkpoint_ref` becomes live (roles phase 2): set it (read-modify-write
  preserving every other field) or show it + best-effort brief. **Role
  claim → resume:** `roles claim X` and `connect --role X` print the
  claimed role's checkpoint ref + brief — the role's "where I left off"
  survives every session death.
- **`fulcra-coord park`** — best-effort session-exit checkpoint of every
  held role: writes a continuity checkpoint per role via the optional CLI
  (timeout-bounded), publishes it to the bus, updates the role's
  `checkpoint_ref`. NEVER exits nonzero; missing CLI / no held roles / bus
  failures are silent no-ops. The Claude Code PreCompact + SessionEnd hooks
  call it BACKGROUNDED and before their session-task early-exits, so a
  session that holds a role but owns no coord task still parks — and a
  continuity problem can never block a session exit.
- **Decoupling pins (tested):** coord never imports `fulcra_continuity`
  (AST fitness test — the schema belongs to that package; subprocess is the
  only seam), and refs are opaque strings forwarded verbatim. Directive
  records grow OPTIONAL additive `checkpoint_ref`/`checkpoint_inline` keys
  (the `_LOOP_KEYS` mixed-fleet floor: pre-continuity records stay valid).

### Version self-incorporation: the fleet stays current from a bus pointer (version-stamped 0.15.3)

**Why:** operator directive (2026-06-10): "i'm not going to go around and
wake the entire fleet for each incremental upgrade." Every release needed a
manual "UPDATE NOW" broadcast plus per-host hand-holding; hosts that missed
it silently froze on an old subcommand set. Now the bus carries a canonical
version manifest and every host incorporates new releases automatically —
**default ON** (an explicit operator call, superseding the reconciled spec's
gated/opt-in note), env opt-out `FULCRA_COORD_SELF_UPDATE=0`.

**What:**
- **Version manifest on the bus** (`runtime/version.json`, schema
  `fulcra.coordination.version.v1`): published by the new maintainer command
  `fulcra-coord announce-version` at each release — announces the INSTALLED
  `__version__` + best-effort `git rev-parse HEAD`, verify-after-write
  (post-upload stat, the writepipe pattern). **The pointer rule** (the
  reconciled spec's non-negotiable safety boundary): the manifest is a
  version *pointer* — version string + commit sha + optional
  `min_supported` — never code, commands, or URLs; its validator REJECTS
  any extra key (pinned by test), and a malformed/tampered manifest reads
  as "never behind" (fail-closed).
- **The check** (`fulcra_coord/selfupdate.py`): `connect` (session start,
  unthrottled, BEFORE the presence write) and the `notify-inbox` listener
  tick (throttled, one check per `FULCRA_COORD_SELF_UPDATE_INTERVAL_H`,
  default 6h) compare the installed version against the manifest. Behind →
  run the update from **local config only**: `update-cmd.json`
  `{"cmd": [...], "cwd": ...}` wins; else `update.json`
  `{"checkout": "/path/to/fulcra-tools"}` drives the built-in default
  (`git -C <checkout> pull --ff-only` + `uv tool install --reinstall
  --force <checkout>/packages/fulcra-coord`) with argv built in code from
  the configured path — nothing read off the bus ever reaches an exec
  boundary. Bounded 300s/step, output appended to `<cache>/self-update.log`.
  A successful update takes effect on the NEXT invocation (no re-exec).
- **Visible degradation, never breakage:** behind-but-can't-update (no
  config, or the update failed) warns once, writes a local stale marker,
  and the presence summary carries `(vX behind canonical Y)` on the roster.
  Both call sites are fully best-effort — a self-update problem can never
  fail a session boot or a polling tick.
- **Rollout:** 0.15.3 is the first self-propagating release — once a host
  is on 0.15.3+ (one last manual update) it incorporates every later
  release automatically.

### Host wake-exec: the listener can wake an agent runtime, not just notify

**Why:** operator directive (2026-06-10): "that needs to be part of the
product. this can't die if i do other stuff for a bit. the whole point was to
enable multiple simultaneous workflows better." The host listener
(launchd/cron `notify-inbox`) detects actionable bus work but could only
NOTIFY — when the operator was away and a session was idle/dead, directives
and review verdicts sat unprocessed. In-session watchers die with the session;
only Codex had an exec-style self-heal (`ensure-codex-watch`), and even that
arms a listener rather than starting a runtime.

**What:**
- **Core mechanism** (`fulcra_coord/wake.py`, platform-neutral — pinned by a
  grep test, zero agent-runtime strings in core): every `notify-inbox` tick
  with pending work consults the optional per-adopter
  `${XDG_CONFIG_HOME:-~/.config}/fulcra-coord/wake.json` (longest-prefix match
  on agent id; fail-safe loader modeled on `review-routing.json`) and spawns
  the configured `cmd` DETACHED, with `FULCRA_COORD_AGENT` +
  `FULCRA_COORD_WAKE_PENDING` in its env and output appended to the listener
  logs dir (`wake-<agent-slug>.log`). Safety rails: per-agent
  `min_interval_min` throttle marker (armed only after a successful spawn),
  single-flight pidfile (a still-running wake skips the next), full
  try/except so a wake problem can never break a polling tick. No config →
  byte-for-byte the old notify-only behavior. Runaway protection is the
  spawned command's own job (`max_runtime_s` is documented intent; core ships
  no process manager).
- **Adapter** (`install-claude-code --with-wake`): seeds this agent's
  wake.json entry with a documented placeholder command (`claude -p "BUS
  WAKE: …process your fulcra-coord inbox…then exit"`), merging surgically —
  other agents' entries and an operator-customized entry for the same agent
  are never clobbered; uninstall removes only this agent's key. Prints a loud
  post-install note: the command runs UNATTENDED with the host's default
  permissions — review wake.json (it is the customization point for binary
  path / permission + timeout flags).
- `wake.example.json` ships in the repo with claude-code / codex / openclaw
  entries clearly marked as EXAMPLES.
- Test hermeticity: the suite now isolates `XDG_CONFIG_HOME` too — otherwise
  a test driving a notify-inbox tick on an operator machine could read the
  REAL wake.json and spawn a REAL agent runtime mid-suite.

### Single task writes retry once and verify delivery (silent single-write loss)

**Why:** Live evidence (2026-06-10, four occurrences in one evening): under
backend write-throttling, single task/directive writes (`tell` / `later` /
`done`) intermittently failed AFTER the CLI printed success-looking output —
the body never landed on the bus, the local cache held it, and it self-healed
only at a much-later reconcile (minutes to hours). Senders believed messages
were delivered; recipients never saw them (a review-queue directive, a `later`
backlog item, two loop closes). At least one case printed the directive-created
banner with **no** cached-locally warn, meaning the upload returned ambiguous
success with nothing on the bus. The reconcile-pool retry
(`FULCRA_COORD_UPLOAD_RETRY`) covers VIEW uploads only — the AUTHORITATIVE
task-body upload in `writepipe._write_task_and_views` had a single, unverified
attempt.

**What:**
- The task-body upload (the authoritative write — best-effort view/event/
  directive side-writes unchanged) retries ONCE after a 0.5–2.0s jitter sleep
  on a False **or raising** upload. New knob `FULCRA_COORD_WRITE_RETRY`
  (default `1`; `0` disables). A second failure is final and falls through to
  the exact pre-existing cached-locally path (return False, marker
  failed+needs_reconcile, caller warns).
- **Verify-after-write:** after a successful-looking upload, the existing
  post-write version-tracking `remote.stat` doubles as delivery verification
  (no extra round-trip on the fast path). A `None` stat — file absent or stat
  failed, both meaning delivery cannot be claimed — triggers one more jittered
  re-upload + re-stat; if STILL unverified, an unmissable
  `DELIVERY NOT CONFIRMED: <task-id>` warning is printed to stderr, the op
  marker is kept with `needs_reconcile` (so the standard reconcile self-heal
  owns the repair, same vehicle as a failed upload), and the ops log records
  `status=unverified`. The command's exit code NEVER flips for an unverified
  write that cached locally — the self-heal contract stands, but the sender now
  SEES it. New knob `FULCRA_COORD_WRITE_VERIFY` (default `1`; `0` disables for
  backends whose stat is unreliable, without losing the upload retry).
- Test conftest no-ops the jitter sleep by default (the `false` safety-net
  backend fails every first upload, which would otherwise add ~70s of pure
  sleep to the suite); jitter-asserting tests patch `writepipe._retry_sleep`
  themselves.

### Roles as durable identity: registry, leases, vacancy escalation

**Why:** Sessions are ephemeral (Claude Code / Codex sessions die, sleep, get
respawned) and identity drifts with them — directives addressed to dead
session ids, role-holding that evaporates when a session does, routing
knowledge announced as broadcasts nobody machine-reads. The 2026-06-10 spec
inverts the model: **the ROLE is the durable identity; a session is an
ephemeral lease on it.**

**What:**
- **Role registry** (`roles/<name>.json`): `fulcra-coord roles set <name>`
  records what a role is for, its `standing_instructions` (the job description
  any fresh claimer follows), `policy` (`shared` fan-out / `exclusive`
  single-holder), `sla_hours`, and `maintainer` (the vacancy escalation edge).
  `checkpoint_ref` is reserved for the continuity phase (claim → resume).
  Zero role names ship in core — roles are adopter data (pinned by a
  generalization test).
- **Leases ride presence** (`roles/<name>/leases/<agent-slug>.json`): one
  file per holder, so claims never clobber each other on the CAS-less bus and
  a re-claim refreshes only the claimer's own lease. A lease is fresh iff its
  holder's PRESENCE is fresh — no new heartbeat machinery; a dead session's
  leases lapse with its presence. `connect --role X` now also claims X
  (additive; the `capabilities` field and routing are unchanged). Claims on
  unregistered roles self-register a minimal record, so a fresh bus never
  rejects a session boot.
- **Vacancy is the new dark-agent signal**: `board` gains a Roles section
  (`HELD by <agent>` / `VACANT <duration>` ⚠ past SLA / `CONTESTED` when an
  exclusive role has >1 fresh lease), and reconcile's health record gains
  `role_health` (report-only, mirrors `loop_health`). A role vacant past its
  `sla_hours` escalates a directive to its **maintainer** — once per day via
  a first-writer-wins marker, not once per reconcile tick. Lease-freshness
  reads ride the staleness-guarded presence loader (below), so a live holder
  can never render dead under backend throttling.
- New `roles.py` (pure folds, stdlib-only, thresholds injected by parameter)
  + `role_ops.py` (best-effort I/O: claim/release/read + registry CRUD with
  verify-after-write), following the loops.py/loop_ops.py split and
  fitness-pinned the same way.

### Staleness-guarded reads: stale views fall back to direct listings

**Why:** Live 2026-06-10 evidence (the highest-severity find of that night's
systematic debugging): every read surface (`inbox`, `presence`/liveness,
`board`) trusts materialized views that refresh ONLY when a write/reconcile
successfully uploads them. Under backend write-throttling (20–80% upload
failures per tick), the views went HOURS stale while task bodies landed fine —
so every agent polling `inbox` saw nothing (6 review verdicts, 2 direct
messages, and a review request sat invisible), and `request-review` reported
"no reviewer live" while the reviewer WAS live (stale presence aggregate). The
durable Tier-0 layer worked; the read path lied.

**What:**
- `views/summaries.json` and `views/presence.json` are now stamped
  `generated_at` (ISO Z) at build time. Additive — old readers ignore it, and
  a view without the stamp (older CLI) is trusted exactly as before.
- `_load_task_summaries` (the fast path under `inbox`/`needs-me`/`status`/…)
  checks that stamp: older than `FULCRA_COORD_VIEW_STALE_MIN` (new env knob,
  default 20 minutes; `0` disables) → ignore the view and read the durable
  `tasks/` files via a RAW listing (`_load_all_tasks_by_listing` — not the
  index/next/search views, which go stale together with summaries), with a
  `WARN` so staleness is visible, not silent. Deliberately unbounded: slower
  but complete — no cap may silently drop tasks.
- Same guard for the presence roster: liveness-sensitive consumers
  (`request-review`, the review-routing reconcile sweep, `tell
  --route-capability`, `presence`) now read through
  `presence._load_presence_agents`, which falls back to listing the per-agent
  `presence/*.json` records (the same enumeration the reconcile rebuild uses)
  when the aggregate is stale.
- Degraded-not-blind floor: if the direct listing ALSO fails (or returns
  empty — indistinguishable from a backend without a working `list`), the
  stale view is still used, with a louder warn. Stale data beats no data.

### Reconcile view uploads retry once with jitter under burst throttling

**Why:** Live 0.15.0 evidence (two hosts): every `reconcile` tick was failing a
ROTATING subset of view uploads under its parallel upload burst (run 1: 13
`inbox/*` views; run 2: index + 6 `workstreams/*`; different sets each run),
while single raw uploads of the same tiny payloads succeeded in <1s — classic
backend throttling / transient 5xx under burst. Each failed view burned its
timeout and failed the tick. The markers-preserved/exit-1 path self-healed on
the next tick, but with EVERY tick partially failing, views stayed stale and
reconcile sat pinned at its ~90s deadline ceiling.

**What:**
- `cmd_reconcile`'s upload pool retries a failed view upload ONCE after a
  0.5–2s jitter sleep, but only when the jitter + a 1s per-upload budget floor
  + 2s of slack all fit before the global deadline — the deadline remains a
  hard ceiling, and a second failure is final (unchanged failure semantics:
  markers preserved, exit 1). Call-site-local by design: `store.upload` serves
  many single-write callers with their own self-heal discipline, so a
  transport-level retry would silently double every timeout everywhere.
- New env knob `FULCRA_COORD_UPLOAD_RETRY` (default `1`; `0` disables) restores
  the pre-fix single attempt.
- Transport observability: `fulcra_coord_files.store` now records the stderr
  tail (last 200 chars) of the most recent failed upload in a module-level
  `last_upload_error` attribute; reconcile logs it to the local ops log
  (`status=view_upload_failed`) on each final view-upload failure, so a
  rotating-failure tick leaves a diagnosable trace, not just view names.

### `resume` flags PRs you opened but never routed for review

**Why:** A reviewer can only act on a review that was *routed* — `request-review`
creates a `kind:review` directive assigned to a live reviewer, which then shows
in their inbox/resume. But when an author opens a PR and just leaves "review
PR #N" as a free-text `next_action` (or task summary), no directive is ever
created, so the review is assigned to nobody and silently goes unreviewed. This
is exactly how PR #101 sat unreviewed: routed by convention only, on no one's
plate.

**What:**
- New pure helper `views.unrouted_pr_reviews(tasks, agent)`: open tasks **owned
  by** the agent whose title/summary/next_action name a PR (`PR #N`, `/pull/N`,
  `pull request N` — deliberately not a bare `#N`, to avoid issue-ref false
  positives) but that carry no `kind:review` marker (i.e. were never routed).
- `resume` surfaces these first, loudly, with the exact fix command
  (`fulcra-coord request-review N --repo <workstream>`); `resume --format
  json` adds an `unrouted_pr_reviews` array. Read-only, summary-only.
- Rule (docs): opening a PR means running `request-review` — never leave a PR
  review as a free-text next_action, or it reaches no reviewer.

### `install-openclaw` can bundle the durable bus-pickup path

**Why:** `install-openclaw` installed OpenClaw's lifecycle hooks, but a fresh
OpenClaw agent still didn't *hear the bus* unless an operator separately ran
`install-heartbeat` + `install-listener`. So "OpenClaw installed" did not mean
"this agent hears directed work" — directed work could go unanswered until
someone noticed the missing scheduler jobs.

**What:** `install-openclaw` can now bundle the heartbeat + per-agent listener
(the durable bus-pickup path) in one command via `--with-heartbeat`
`--with-listener` `--agent <id>` (plus `--heartbeat-interval-min`,
`--listener-interval-min`, `--schedule-target-dir`, `--logs-dir`). It composes
the already-hardened `install_heartbeat` / `install_listener` (inheriting their
PATH-safe CLI resolution + per-agent slug semantics) rather than open-coding
launchd/cron. This is the OpenClaw analogue of the `ensure-codex-watch`
self-heal. As part of the change, the command was restructured so its add-on
blocks run in all three modes (install, dry-run, uninstall) — previously the
early `return 0` on dry-run/uninstall made even the existing `--with-plugin`
block unreachable in those modes; that is now fixed too.

### `ensure-codex-watch`: Codex coordination self-heals on every app start

**Why:** Codex's durable per-agent inbox listener was only ever installed if an
operator manually ran `install-listener`. On a fresh Codex machine that never
happens, so Codex silently never heard directed work on the bus — it had hooks
but no listener, and nobody noticed until directed work went unanswered.

**What:** a new `ensure-codex-watch` command — the single idempotent "make Codex
coordination self-healing" entry point. It composes the already-hardened
installers (`install_codex` + the per-agent `install_listener`), best-effort
`launchctl load`s the listener plist (`--no-load` to skip; macOS/launchd only),
and optionally refreshes presence (`--no-connect`). The Codex SessionStart hook
now backgrounds it on every app start, so a missing listener self-heals with no
operator action. Fully fail-safe: a failed load or connect is warned but the
command still returns 0 (it runs backgrounded at every SessionStart and must
never hard-fail the hook), and it is safe to run repeatedly (the underlying
installers are idempotent; an already-loaded launchd job is a harmless no-op).
The SessionStart hook keeps its `connect --can-review` (Codex is the canonical
review target) and now derives a `codex:*` fallback identity instead of
`claude-code:*` so a pre-handshake box arms the right agent's listener.

---

## [0.9.1] — `remote.list_files` normalizes the real CLI output to clean paths

**Why:** `remote.list_files(prefix)` returned the raw `fulcra file list` display
lines verbatim. The real `fulcra file` CLI formats each line for humans as
`"383B    2026-06-06 01:52AM UTC  claude-code-mac-fulcra-coord.json"` (size +
date + FILENAME-only) — that string is not a path. Every list-based consumer then
fed it straight into `remote.download_json` / `remote.delete`, which silently
returned None / no-op in LIVE: self-heal (`_load_summaries_for_rebuild`'s
vanishing-directive recovery, `_reconcile_presence`), presence prune/reconcile,
retention pruning of dead health/presence records and digest markers,
`search --archived`'s archive index, and the new 0.9.0 health command all read
nothing. The whole test suite stayed green because the fake backend emits clean
full paths, never the real formatted shape — so the bug was invisible to tests
and only surfaced by live-smoking the 0.9.0 health command.

- **`remote.list_files` now normalizes each output line to a clean full remote
  path**, robust to BOTH the real CLI's formatted line and an already-clean path.
  It takes the last whitespace-delimited token as the filename (safe because
  filenames are slug/id-based with no spaces), passes through anything already a
  full path or already prefixed, and otherwise joins the bare filename onto the
  prefix. The real CLI and the fake backend now converge on identical clean paths.
  Best-effort contract preserved (returns `[]` on error). No consumer changed —
  they were all already correct given clean paths.

---

## [0.9.0] — Coordination-system health surface: a silently-failing reconcile becomes visible

**Why:** the operator had rich awareness of *task* state but ZERO awareness of
whether the coordination machinery itself was healthy. A real incident: reconcile
failed on every heartbeat (a `KeyError` on a malformed bus task), views silently
went stale, retention never ran — and nothing surfaced it. It was caught only by
a manual live smoke. This release makes a degraded bus visible.

- **Self-reported per-host health record.** On a SUCCESSFUL reconcile (views
  rebuilt + uploaded with `failures == []`), each host writes
  `health/<slug>.json`. Staleness of that self-report IS the degradation signal
  (the same mechanism as presence liveness). The write is a separate,
  failure-isolated upload placed AFTER reconcile's `if failures: return 1` guard —
  so a failed reconcile leaves its record stale (the contract), and a flaky
  best-effort sub-pass can't suppress a healthy heartbeat.
- **`views.assess_infra_health` (pure).** Judges newest `reconcile_at` per host:
  healthy / degraded (default = heartbeat interval × 3) / outage (default ~3h).
  A host with no record = "not reporting" (informational, never a false alarm).
  Duration / repair-backlog / bus-size are surfaced as metrics, NOT gated.
- **`fulcra-coord health [--format table|json]`** dashboard + a fleet-health fold
  into `doctor` + a one-line infra summary in the twice-daily operator digest
  (the digest scheduler is independent of reconcile, so it reports a broken
  reconcile even on a single-host box — v1's push surface).
- **Retention prunes `health/`** on the dead-presence window, so a decommissioned
  host's records disappear in lockstep with its presence record.
- **Knobs:** `FULCRA_COORD_HEALTH_DEGRADED_SECONDS`,
  `FULCRA_COORD_HEALTH_OUTAGE_SECONDS`.

---

## [0.8.3] — Reconcile + write paths survive imperfect bus data; review sweep is deadline-bounded

**Why:** an adversarial sweep of the 0.8.2 bus surfaced a family of crashes that
all share one root cause — code that bracket-indexes a task/aggregate body for a
field (`id`, `agent`) that an older or imperfect write can omit. Because several
of those accesses sit on the **reconcile** and **write** paths (which run on every
heartbeat tick and every create/update/done/tell), a single malformed body could
take down the whole tick — the same heartbeat-outage class as the just-fixed
`build_search_index` incident. This release makes those paths tolerant of a body
missing a field, and additionally bounds the review-route sweep to the reconcile
deadline so it can no longer starve the retention pass.

- **reconcile no longer crashes on an id-less active expired-claim body (A1).** The
  stale-claim scan in `cmd_reconcile` used `t["id"]` with bracket access; a real
  body with `status=="active"` + an expired `claim.claim_expires_at` but **no
  `id`** raised an uncaught `KeyError` (the inner `try` only guarded `ValueError`
  from `fromisoformat`) — and it ran *before* `build_all_views`/upload, so the
  whole reconcile aborted and every heartbeat tick failed. The loop is now a
  dedicated `_detect_stale_claims(all_tasks, now)` helper that skips id-less
  bodies and parses the expiry via `views._parse_dt` (never lexical).

- **every write command no longer crashes on an id-less cached body (A2).**
  `_load_all_tasks` built `{t["id"]: t for t in cached}` and
  `task_map[t["id"]] = t` with bracket access; an id-less cached body raised
  `KeyError` that propagated uncaught through `_load_summaries_for_rebuild` →
  `_write_task_and_views`, crashing create/update/done/tell/… Both now skip a body
  with no `id` (it has no stable key anyway) and keep the well-formed tasks.

- **`presence` tolerates an agent-less aggregate entry (A3).** `cmd_presence`
  rendered `f"... {a['agent']} [{a['liveness']}] ..."` with bracket access, but
  `build_presence` carries records through verbatim and never injects `agent` — so
  an agent-less aggregate entry crashed the command. It now uses `.get(...)` and
  the well-formed entries still render.

- **the review-route sweep is deadline-bounded (B1).** `_sweep_review_routes`
  loops O(review-directives) with a per-item network fetch + potential full
  view-rebuild write and had **no** time check, so it could run past reconcile's
  ~90s deadline and starve the retention pass (which already gates on the
  deadline). It now threads reconcile's `deadline` through and stops processing
  further directives once the budget (minus a small headroom) is spent, logging
  how many were deferred. Best-effort: deferred directives drain next tick;
  `deadline=None` keeps the old unbounded behavior for direct callers.

- **archive verifies the hot copy before moving (B2).** `_archive_task` would
  upload a phantom archive body + shard for a task that existed **only** in this
  host's stale local cache (deleted remotely by another host, never archived
  here). It now requires a positive stat of the hot `tasks/<id>.json` before the
  move — unless the archive body already exists (the idempotent / crash-recovery
  finish) — and otherwise skips the phantom and evicts it from the local cache so
  it stops being reloaded. Mirrors the reroute sweep's `if fresh is None: continue`
  guard; idempotency and the crash-safe ordering are unchanged.

All best-effort paths still never raise into a reconcile/scheduled tick. Datetime
comparisons go through `views._parse_dt` (never a lexical string compare).

---

## [0.8.2] — Hermetic test cache: tests can no longer pollute the real bus

**Why:** `cache.cache_root()` resolves to `${XDG_CACHE_HOME:-~/.cache}/fulcra-coord`.
Any test that hit a cache-writing path (`write_cached_task`, `_write_task_and_views`,
`cmd_reconcile`, `_sweep_review_routes`, …) *without* first redirecting
`XDG_CACHE_HOME` wrote straight into the **operator's real** `~/.cache/fulcra-coord`.
Most test classes set `XDG_CACHE_HOME` by hand in `setUp`, but several did not —
notably the reviewer-routing sweep tests — and their fixtures (`author:h:r`,
`dead:h:r`, a title-less `TASK-20260604-rev-00000000`) leaked into the real cache.
`reconcile` then read that polluted local cache and **pushed the junk tasks to the
live coordination bus**, where they crashed reconcile. A prior run left 127 stray
tasks in a developer's real `~/.cache` the same way. This is a correctness fix:
the test suite must never touch the operator's cache or bus.

- New `tests/conftest.py` with an **autouse, function-scoped** fixture that points
  `XDG_CACHE_HOME` at a fresh per-test temp dir for **every** test and restores the
  prior value afterward. The cache is now hermetic by default — no test can write to
  the real `~/.cache/fulcra-coord` regardless of whether it remembered to isolate.
  Function scope (not session) so tests never share cache state, matching the
  per-test isolation the careful tests already did manually.
- The same fixture defaults `FULCRA_COORD_BACKEND` to `false` when unset, so a test
  that reaches an unmocked remote file-op can't shell out to the real `fulcra` CLI
  and touch the live account. Tests that inject their own backend (the stateful fake,
  or an explicit `backend=` arg) override this freely — no existing test changed.
- Added `TestCacheIsolationHermetic`: proves `cache_root()` resolves under the temp
  dir (never the real `~/.cache`) and that a cache write from inside a test leaves the
  real home cache byte-for-byte unchanged.
- Existing per-`setUp` `XDG_CACHE_HOME` juggling is left in place — it's now harmless
  (it runs inside the fixture's redirected world) and removing it would be churn.

## [0.8.1] — Reconcile no longer crashes on a malformed task

**Why:** `build_search_index` read `task["id"]` / `task["title"]` with bracket
access. A single real task body missing `title` (or `id`) raised `KeyError`
straight out of the aggregate rebuild — aborting the entire `reconcile`: views
weren't repaired and the new retention pass never ran (its marker was never
written). Caught by a live smoke of 0.8.0 against the real 140-task bus
(`ERROR: 'title'`), which a from-scratch test env never exercises.

- `build_search_index` now uses `.get()` defaults for every field (a malformed
  task surfaces in the search index with empty strings instead of crashing the
  rebuild) — the same render-don't-crash contract `task_summary` got in the
  0.5.6 debug sweep. Regression test added.

## [0.8.0] — Bus Retention / Archival

**Why:** The coordination bus grew without bound. Terminal (done/abandoned)
tasks stayed under `tasks/` forever — bloating `views/summaries.json` (the
aggregate behind the read perf win), growing the `tasks/` listing self-heal
enumerates on every write, and swelling recently-done/search. Digest markers and
dead-agent presence records also accumulated. Reads and reconcile slowly
degraded and the operator surfaces got noisier; nothing removed anything.

**What:**
- Terminal tasks aged past `FULCRA_COORD_RETENTION_DAYS` (default 30) are
  crash-safely MOVED to `archive/tasks/<YYYY-MM>/<id>.json` with an append-only
  per-id cold-index shard `archive/index/<id>.json` (no shared mutable index —
  Files has no CAS). Moving the body out of `tasks/` removes it from the
  aggregate, views, and self-heal automatically — zero read-path filter code.
- `search --archived` (alias `--all`) scans the cold index; default search stays
  hot-only and fast. `restore <id>` moves an archived body back into `tasks/`.
- Spent digest markers (>`FULCRA_COORD_MARKER_RETENTION_DAYS`, default 7) and
  dead-agent presence (>`FULCRA_COORD_PRESENCE_RETENTION_DAYS`, default 30) are
  soft-deleted via `fulcra file delete` (platform-restorable). Both prune gates
  fail SAFE: an undatable marker or presence record is kept, never deleted.
- The pass is folded into `reconcile`, self-throttled to ~once/day via a
  first-host-wins `retention/last-run.json` marker (the digest-marker pattern) —
  no new scheduler. Bounded by `FULCRA_COORD_RETENTION_MAX_PER_RUN` (default 200)
  + a time budget that composes with reconcile's deadline; best-effort
  (never raises into a tick). No data loss by construction (write→verify→delete).

**How tested:** new `tests/test_retention.py` — policy predicates (cutoff
boundaries, non-terminal exclusion), crash-safe move (write→verify→delete order,
crash-mid-move completion, idempotency), append-only shards, `search --archived`
/ `restore`, throttle + cap + time-budget + best-effort, marker/presence prune,
and a VERIFIED automatic-hot-path-exclusion test that archives a terminal task
and asserts it leaves the rebuilt `tasks/` listing and summaries with no filter.

**Fix (local-cache resurrection):** the archive MOVE deleted the remote
`tasks/<id>.json` but left the body in the *local* cache of the host that ran the
archive. Because `_load_all_tasks` (the reconcile load path) seeds its task map
from `cache.list_cached_tasks()` and only ever ADDS remote ids — never removes —
that host's very next `reconcile` rebuilt the archived task straight back into the
authoritative `views/summaries.json`, re-surfacing it across the fleet (and it
stayed an archive candidate forever, re-archived as an idempotent no-op each day).
The hot-path exclusion held for *other* hosts but not the archiving one.
`_archive_task` now evicts the local cache entry (`cache.delete_cached_task`) as
the final step of a verified move. Covered by a unit test (eviction) and an
end-to-end `reconcile` regression test that archives a locally-cached terminal
task and asserts it does not reappear in the rebuilt summaries.

---

## [0.7.0] — Liveness-Aware Reviewer Routing

**Why:** PR-review directives were routed to a FIXED reviewer (canonical, or a
configured #devops fallback) regardless of whether that agent was online. PRs
sat unreviewed in a stale fallback's inbox while a capable reviewer was idle the
whole time, and nothing re-routed a directive once its assignee went dark.

**What:**
- `request-review <pr> --repo <repo>` routes a PR review to a reviewer presence
  says is actually live/idle (capability-based pool: canonical reviewer seed +
  agents that declared `--can-review`), tagging the directive `kind:review` and
  recording a `routed` event. `--dry-run` shows the ranked pool/tiers/winner.
- `connect --can-review` / `--role` declare an agent's capabilities on its
  presence record (default `[]`, backward compatible).
- `reconcile` now sweeps stalled `kind:review` directives: re-routes a never-
  acted review whose assignee fell below liveness floor (P1 15m / P2 30m,
  env-overridable; cap 2; then escalate to the human), and freezes one the
  assignee explicitly accepted, escalating only after a long stall.
- `tell --route-capability R [--floor live|idle]` exposes the underlying
  route-to-live primitive for any directive.
- Escalation (no live reviewer) lands on the human's plate via the existing
  `block --on-user` / needs:human surface. New env knobs:
  `FULCRA_COORD_PRESENCE_GRACE_SECONDS` (1200), `…REVIEW_REROUTE_MINUTES_P1/P2`
  (15/30), `…REVIEW_REROUTE_MAX` (2), `…ACCEPTED_STALL_HOURS` (2).

---

## [0.6.0] — Operator Digest

**Why:** the human surface was pull-only — you saw "what's blocked on me" / "what
is everyone doing" only when you started a session or ran needs-me/agents/resume.
Between sessions you were blind, and the granular per-event annotations were too
fine-grained to read as a glance. The Operator Digest is the push side: a
consolidated, human-paced situational-awareness summary delivered to your Fulcra
timeline twice daily and on demand.

- **`fulcra-coord digest [--window morning|evening] [--format json] [--dry-run]`** —
  builds a four-block digest from existing bus state + presence (blocked on you,
  upcoming, what each agent did since the last window, what's stale) and writes it
  to the timeline. `--dry-run` renders without writing; `--format json` prints the
  structured digest.
- **New "Agent Tasks — Digest" timeline track** — a second moment definition,
  separate from the granular per-event "Agent Tasks" track (which is kept,
  untouched), so digests filter on their own.
- **Any-agent, dedup-guarded** — any machine can run the digest; a first-writer-wins
  marker (`digest/markers/<date>-<window>.json`) collapses concurrent runs to one
  digest per window (a rare same-second double is accepted as harmless, since
  Fulcra Files has no compare-and-swap).
- **`fulcra-coord install-digest`** — schedules the digest twice daily (launchd
  08:00/18:00 on macOS, cron elsewhere). Safe to install on every machine.
- **Per-event annotations now carry work substance** — the note reads
  `[<workstream>/<kind>] <title> — <summary> · next: <action>` instead of just the
  lifecycle category. Note-body only; backward-compatible.

All digest paths are best-effort: a failed read/marker/emit never raises into a
scheduled tick. Datetime comparisons (the `since` window + due ranking) parse
timestamps (consistent with the 0.5.x mixed-precision fix), never lexical compare.

## [0.5.6] — Debug sweep, rounds 2-3

**Why:** a second adversarial pass focused on timestamp precision, malformed task
bodies, human-blocking tags, annotation cache drift, and hook command hygiene.

- **Timestamp precision:** all new coordination timestamps now emit fixed-width
  microseconds, and freshness decisions parse datetimes instead of comparing raw
  strings, so mixed-precision values already on the bus cannot silently drop the
  newer side of a merge or rebuild.
- **Malformed task bodies:** a cached task body with missing display fields now
  surfaces in rebuilt views with empty-string defaults instead of vanishing from
  every materialized view.
- **`needs:human` cleanup:** assigning a human-blocked task away from the human
  strips the stale `needs:human` tag; assigning it back to the human preserves
  the marker.
- **Annotation cache TTL:** cached definition/tag ids expire after 24h by
  default (`FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS` override), bounding drift
  after server-side deletes or renames while keeping annotation emission
  best-effort.
- **Hook command hints:** SessionStart resume hints shell-quote the resolved
  `fulcra-coord` command and agent id before printing copy-pasteable commands.
- **Reviewer-caught (codex):** the summaries rebuild path had one remaining raw
  `updated_at` string compare; it now uses the same parsed timestamp key as the
  merge path.

## [0.5.5] — Reconcile performance

**Why:** `reconcile` ran serially — each task's body load and each materialized
view upload happened one round-trip at a time — taking ~96s end to end. That
overran the heartbeat's timeout, so the heartbeat kept dying and presence/views
went stale.

- Per-task body loads and view uploads now run in parallel
  (`ThreadPoolExecutor`), cutting reconcile from **~96s to ~23s** and
  un-breaking the timing-out heartbeat. Partial-failure, timeout, and exit
  semantics are preserved — a failed leg still degrades gracefully rather than
  aborting the whole pass.
- **Deadline fix (reviewer-caught, codex):** each parallel view upload was
  handed the full per-call timeout instead of the *remaining* budget, so an
  upload starting near the limit could still block for the full timeout and
  blow the overall reconcile deadline. Now a global deadline
  (`t0 + timeout`) is computed once and each upload gets `max(1, remaining)`,
  skipping outright once the deadline has passed.

*(0.5.2 was not released standalone — the branch version bumped to 0.5.5 during
the stacked merges below.)*

## [0.5.4] — Debug sweep, round 1

**Why:** a focused audit of the merge / optimistic-concurrency and self-heal
paths turned up 12 confirmed bugs where state could be silently dropped,
misclassified, or mis-timed.

- **Merge / optimistic concurrency:** `_try_merge` now carries forward fields it
  was dropping and unions the `event` / `acked` collections instead of letting
  one side overwrite the other.
- **Summaries aggregate:** the rebuild self-heals a dropped directive rather than
  persisting the loss.
- **needs_human / scheduling gates:** datetime comparisons now parse before
  comparing and coerce naive timestamps to UTC, so `not_before` / `due` gates
  fire on the right boundary instead of throwing or comparing apples to oranges.
- **Inbox auto-aging, presence:** both self-heal correctly under the cases the
  sweep exercised.
- **Annotations HTTP writer:** the tag-id cache and `recorded_at` anchoring were
  corrected so timeline moments land at the right time with the right tags.
- **Stale derived-tag repair (reviewer-caught, codex):** a safe merge that
  combines local status with newer remote fields left behind a stale
  `status:<old>` tag, misclassifying the task in tag-filtered views.
  **`_repair_merged_tags`** rebuilds the standard tags from the merged fields
  while preserving non-standard ones (e.g. `needs:human`).

## [0.5.3] — Per-agent listener

**Why:** the listener's launchd/cron identity was machine-global
(`com.fulcra.coord.listener`), so two agents co-located on one host clobbered
each other's listener job — installing one tore down the other's.

- The listener label / plist / cron-marker now derive from the agent slug
  (`com.fulcra.coord.listener.<slug>`), so each co-located agent gets its own
  job. Install, uninstall, and the cron strip are all agent-scoped.
- A legacy un-slugged plist is **superseded only for the agent it watched**, and
  symmetrically on both install **and** uninstall — a reviewer-caught (codex)
  install/uninstall asymmetry that would otherwise have left an orphaned legacy
  job behind.
- The **heartbeat stays a singleton** — only the listener is per-agent.

## [0.5.1] — Inbox auto-aging

**Why:** informational broadcasts ("X joined the mesh", "identities live") linger
as proposed directives in every agent's inbox forever (broadcasts never get
"done"), piling up and burying real directives at SessionStart.

- The inbox view now hides a **broadcast** (`assignee="*"`) that's still
  `proposed` and older than `FULCRA_COORD_INBOX_AGE_DAYS` (default 3). Pure
  read/view filter — task status, the task file, and the aggregate are untouched
  (nothing is abandoned; a peer on an older CLI still sees it).
- **Directives addressed to a concrete agent are NEVER aged out** regardless of
  age — a real ask stays until acked/done.
- `inbox --all` shows everything; the default prints `(N older broadcast(s)
  hidden — --all to show)`. The SessionStart banner inherits the filter.

## [0.5.0] — Scheduling for the "blocked on you" plate

**Why:** an agent could block a real task on the human that wasn't *actionable
yet* — e.g. "re-auth this 7-day OAuth token" five days before it expires. The
human then saw "⛔ BLOCKED ON YOU" at every session for days for something they
couldn't do. Operator's words: *"every agent keeps telling me about this task but
it's not relevant yet."*

- **`block --on-user "<ask>" [--not-before <when>] [--due <when>]`** — schedule
  when an ask becomes relevant. `<when>` accepts an ISO date/datetime or a
  relative offset (`5d`, `36h`, `10m`).
- **`needs-me`** now shows only **due-now** items, then a compact
  **"Upcoming (next 7d)"** section (`[in 4d] … (due Jun 8)`). `--all` expands
  upcoming inline; `--format json` returns `{human, count, items, upcoming}` with
  `count` counting due-now only.
- **SessionStart banner** counts only due-now items in the `⛔ BLOCKED ON YOU (N)`
  headline and appends a muted `(+N upcoming)` — a future-only plate shows no
  alarm headline at all.
- New optional task fields `not_before` / `due` (carried through `task_summary`,
  so view rebuilds stay equivalent). Backward-compatible: tasks without them
  behave exactly as before.

## [0.4.1] — Self-heal: stop directives vanishing from the bus

**Fixed (critical):** under concurrency, directives could silently disappear from
`inbox` / `status` / `agents` for minutes. The performance work (0.4.0) rebuilt
views from the single `views/summaries.json` aggregate; being one object under
last-writer-wins, a concurrent peer's write could clobber it and **drop a task
another agent had just created** — and because every later write re-read the
clobbered aggregate, the drop persisted until a (90s) `reconcile`.

The per-task files (`tasks/<id>.json`) are the durable, un-clobberable truth. The
write path now enumerates them and re-includes any task whose file exists but the
aggregate dropped — fetching a body only for the rare missing id (≈0 in steady
state). A dropped task now **self-heals on the very next write by any agent**
(seconds), not a reconcile cycle. Add-only and best-effort: a failed/empty
listing can never make the rebuild worse than the aggregate alone.

## [0.4.0] — Situational awareness, performance, annotations, presence

The big release. fulcra-coord's north star became *the human's situational
awareness — above all, "what's blocked on me."*

### Performance
- **Reads `agents` / `needs-me` / `resume` / `status` went from ~56s to <1s**, and
  writes from ~30s to ~7s. Root cause: every read *and* every write re-fetched
  each task body one file at a time. Introduced a `views/summaries.json` aggregate
  (one read replaces N+3 round-trips), rebuilt views from it instead of re-fetching
  bodies, and parallelized view uploads. `task_summary` was enriched
  (`last_touched_by`, `done_at`, `acked_by`) so `build_all_views(summaries)` is
  byte-identical to building from full bodies (guarded by an equivalence test).

### Situational awareness — "what's blocked on me"
- **Human operator handle** (`human [set|clear]`, default `human`, personalizable)
  — the addressable identity tasks are "blocked on ME" against.
- **Blocked-on-you surface**: `block --on-user "<ask>"` assigns a task to the
  human + tags `needs:human`; **`needs-me`** is the human's plate (who's waiting,
  what they need, how long); the **SessionStart banner leads with ⛔ BLOCKED ON
  YOU**. Broadcasts are excluded — only concrete asks surface, so the banner is
  signal, not noise.
- **`resume [--agent X]`** — a pick-up-where-you-left-off briefing (your active
  work, what's blocked on you, what you owe others, what's blocked on the human).
- **needs-user timeline annotation** + a listener that notifies on new
  blocked-on-you items.

### Agent presence
- **`connect` / `workstream` / `presence`** — agents report their current major
  workstream(s) on connect, so `agents` shows what each agent is working on **even
  with no active task**. The SessionStart hook auto-reports presence.

### Annotations (now actually work)
- Lifecycle and needs-user moments write to the operator's **Fulcra timeline via
  the HTTP API** (the path fulcra-collect uses), implemented in pure stdlib —
  resolve tags → resolve/create the "Agent Tasks" moment definition (cached) →
  `POST /ingest/v1/record/batch`. Enable durably with **`annotations on`**
  (persisted), or per-shell `FULCRA_COORD_ANNOTATIONS=http`. `doctor` reports the
  mode + token state.

### Identity & operability
- **Per-cwd identity** (realpath-keyed) so sibling sessions on one machine stop
  clobbering each other's id; legacy global `identity.json` is no longer
  auto-read (`identity migrate` to adopt it).
- **`--version`** flag + **`capabilities`** probe (advertises supported commands)
  + a dynamic version sourced from one place, so `uv tool install --force`
  rebuilds reliably.
- **`doctor` checks for the `file` command group** — the #1 fresh-agent trap
  (public PyPI `fulcra-api` lacks it), with the exact fix.
- `start` no longer requires `--agent` (auto-resolves identity like its siblings);
  onboarding hints for the identity-migrate and start-vs-claim cases.

### Conventions (documented for every agent)
- **One git worktree per session** (a shared checkout clobbers index/HEAD).
- **No direct pushes to `main`** — every change is a PR, reviewed by another agent
  (non-Arc Claude → the Codex reviewer; Arc → arc-code-review), merged by its
  author. Repo homes: `fulcra-tools` only for things that make Fulcra useful to
  others; Fulcra infra → `ashfulcra`, personal → `reversity`.

## [0.1.0] — Initial agent coordination layer

- Durable tasks on Fulcra Files: `start` / `update` / `pause` / `block` / `done` /
  `abandon`, with an optimistic-concurrency write + structured merge.
- Cross-agent **directives**: `tell` / `broadcast` and a per-agent `inbox`.
- **`agents`** status digest, materialized views, `search`, `reconcile`.
- Lifecycle **hooks** (Claude Code SessionStart/PreCompact/SessionEnd; Codex;
  OpenClaw) and a durable **listener** + heartbeat installers.
- Adapters/ONBOARD docs for Claude Code, Codex, OpenClaw, and a ChatGPT facade.
