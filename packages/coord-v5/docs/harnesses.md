# In-harness listeners

One reporting listener serves an enrolled environment/harness; addressed role/job workers remain separate and reasoning sessions disposable. Its job is to receive bus traffic, preserve coverage and route attention through locally configured native messaging, not execute the workers' tasks. Prefer bounded subagents for worker reasoning or implementation. A deliberate self-ticking executor may continue its own assigned task, but only as a separate opt-in mode. Store the native listener registration with the private workspace descriptor. A fresh listener reconstructs unfinished obligations from annotations and recovers ambiguous delivery from its receipts before resending.

Verified native event ingress is the preferred enrollment path when dispatch, recovery and scoped read/publish are also current and unattended operation was requested. A schedule can backstop that path; verified ticks or session loops are fallbacks when event ingress is absent, unsupported or unknown. The planner's `native-event` mode does not implement an event adapter or prove that a product supports event ingress. Verify actual reception and idle worker delivery in the enrolled environment before advertising them.

Until a listener's wake has been verified in an environment, each addressed worker there keeps its own native wake — a scheduled run, thread heartbeat or session loop that re-enters it to read its obligations — so no worker goes unattended while the listener is absent, unproven or down. A verified listener lets that worker wake relax to a coarse backstop; it does not remove it. Retire a working wake only after its replacement has been shown to re-enter the worker: a registration, a delivered schedule or an accepted native send proves the mechanism ran, not that the worker did. This obligation-reading wake is not the self-ticking executor mode above — it reads and routes attention; it does not execute owned work.

Generate `instruction_mode:"listener"` through the [instructions command](onboarding.md#capability-assessment)
for this reporting session; use `executor` only for separately authorized owned-task execution.
Keep these role blocks in listener/executor local/session instructions, never shared repository-wide
worker instructions. The default `worker` block is unchanged. Listener reports cover its own reads,
routes, conflicts and reconciliation, never worker claims/progress/completion or invented receiver
acknowledgments. Executor recovery needs explicit ownership and context; a wake cannot authorize
adoption. Mode selection installs nothing: fallback ticks/loops and direct executor self-ticking
each require separate authorization. Generated prose is not runtime enforcement or consumer-agent proof.

## Codex desktop

Use locally verified native thread messaging from an active listener turn. Assess event ingress separately; use native thread heartbeats/scheduled tasks as a verified fallback or backstop, not a mandatory event-path component. Do not configure an external command as though it could call an app-only tool. Local scheduled runs require the app and machine to be available; verify permissions in the actual scheduled turn. See [Codex scheduled tasks](https://learn.chatgpt.com/docs/automations).

Verify the exact native cross-session route locally, including human authorization and target restrictions. An observed Codex ancestor-target restriction is evidence about that route, not a universal prohibition on messaging peer sessions. Keep route identifiers and target mappings private; availability in an active turn does not prove an idle receiver will wake.

The enrollment plan's `listener_scope` uses the dispatcher's `codex` harness key. Keep one private listener SQLite journal for that scope and configure exact job-to-thread routes with `coord-v5 listener configure`. `active` routes target an existing thread; `dormant` routes request a successor; `retired` routes notify their configured coordinator. Successor creation is a harness action, not an implied role grant.

Use this bounded native listener job recipe, substituting only local approved configuration paths and identity:

1. On actual event reception or a fallback tick, read the owned annotation source with `transport read`; obtain both fresh events and durable obligations with `observation`. Preserve partial/unavailable coverage and the durable cursor/dedup state; do not treat a failed read as empty or advance past an unprocessed gap.
2. Pass that observation to `listener prepare` using the registered scope, journal and holder. Inspect its known attention items and prepared actions.
3. Execute only the prepared supported native message action for a locally configured route. Preserve the journal's delivery intent before invoking native messaging. Save `accepted`, `error`, or `unknown` via `listener settle` using the exact wake, attempt and target. Keep that native receipt separate from receiver acknowledgment and task progress/completion. A receiver acknowledges with `listener ack` after matching its target and wake IDs.
4. Report `needs_successor`, `needs_coordinator`, conflicts, failed reads and uncertain delivery to the configured coordinator. For a successor, retrieve role/job checkpoint plus the fresh event tail and verify artifact access before transferring responsibility. Reconcile an uncertain send rather than creating a new identity to retry it.
5. For an event path, keep the verified event registration active. If a native heartbeat is the fallback or backstop, use the returned interval for that heartbeat only. Preserve a faster operator interval. Update through the native scheduling tool while preserving the full prompt, target and notification preferences. Failed or incomplete reads are not quiet time; scheduled retries retain the short error interval. Never cancel the environment listener merely because a workstream finished.
6. Stay quiet for unchanged non-actionable state; notify on meaningful work, failure, completion or user decision. Reasoning sessions publish their own contact/progress/checkpoints; the listener reports only its own successful reads.

The journal's recurrence is a suggestion until the native schedule update succeeds. Record both values. Live idle-wake proof does not establish restart, host-outage, permission-prompt or upgrade behavior; keep those capability fields unknown until tested. Do not restart the user's app or host as part of ordinary enrollment.

## Other environments

### Claude Code: explicit local delivery

The package now supports opt-in `coord-v5 listener dispatch-claude`, using the
canonical enrollment harness `claude-code`. Configure a private
`target:{kind:"claude-code",sessionId:UUID,cwd:ABS}` route and pass the prepared
action's `.arguments` on stdin with trusted `--executable ABS`. Existing Codex
thread routes and native heartbeat steps are unchanged. See the [dispatcher
contract](../README.md#explicit-claude-cli-delivery) for fixed flags requesting
the full model ID `claude-haiku-4-5-20251001` and a $0.25 provider budget,
tool-free execution, 60-second/64-KiB bounds, exact result correlation and safe
failure codes. Exact argv is enforced; served
model and provider billing are not. Inspect actual assistant-model telemetry:
the served model may differ from the requested model ID, and auxiliary `modelUsage`
does not identify the conversational model. Sub-budget success does not prove
a hard spend ceiling.

Preparing is not sending. An accepted native result is not receiver acknowledgment,
source completeness, a schedule, or lifecycle acceptance. The local journal claims
before launch; repeated and ambiguous attempts require reconciliation, including
crash-left claims. Do not resume the same session elsewhere concurrently: the
fence is local to one journal/scope, not global busy detection. The operator must
verify the CLI/version, existing session, trusted CWD and credential availability
without copying them into public artifacts. No sign-in, new-session creation or
force takeover is performed. Fixture and packed-install success are local tests,
not authenticated native acceptance or restart/outage evidence.

### Claude cloud: verify before placing a listener

These platform behaviours decide where a Claude-cloud listener can live and how it wakes a worker. They change faster than this guide (last checked 2026-10-03): probe each one in your own environment and record the result in that environment's acceptance record, not here.

- The minimum interval for a recurring cloud routine (last checked: one hour; shorter schedules were refused). One-shot scheduled runs, including a session's own scheduled follow-up messages, resolve to roughly the minute.
- Whether a scheduled run of a routine bound to an existing session is delivered into that session (last checked: yes, queued while busy) and whether a manual "run now" starts a new session instead (last checked: yes). To wake a specific worker, schedule a one-shot run bound to its session; a manual run hands the work to a fresh session without that worker's context.
- Which connector tools a directly started session receives (last checked: none) versus a session a routine starts with connectors attached. A listener that must schedule wakes for other sessions therefore needs the session-management connector attached through its routine.
- Whether anything written inside a container survives its reclamation (last checked: nothing, including a file-based login). A listener that runs in fresh containers must authenticate through an attached connector or a credential the environment supplies on every start, not through a login performed once in an earlier container.
- Which sessions cross-session messaging can reach from a cloud container (last checked: only sessions on the same machine).

No Claude-cloud listener has passed end-to-end acceptance yet. Until one does, Claude-cloud workers rely on the worker wake described at the top of this guide.

| Harness | Self-assessment path |
|---|---|
| Claude Code live session | Inspect native loop/scheduled-task and channel tools in that session. Verify workspace access, busy delivery, expiration and resume behavior. A session loop shares the session's lifetime. |
| Claude desktop | Inspect desktop-native scheduling and messaging separately from CLI capabilities. Verify the receiving session and its tool permissions. |
| Claude cloud | See [what to verify](#claude-cloud-verify-before-placing-a-listener) above. Inspect routines, attached connectors and the new-session path in the environment itself; rehydrate role/job context rather than assuming a session is resumed. |
| Codex cloud / ChatGPT web | Probe scheduling and connected record/file tools in the scheduled environment itself; desktop tool availability does not prove cloud access. |
| Regular Claude web | Start interactive with verified tools. Upgrade only after an idle invocation and cross-session delivery are demonstrated. |
| OpenClaw / Hermes | Inspect the installed gateway's native scheduling and notification tools; test lifecycle and credential access in that environment. Keep gateway health separate from worker progress. |

Official starting points: [Claude scheduled tasks](https://code.claude.com/docs/en/scheduled-tasks), [Claude channels](https://code.claude.com/docs/en/channels), [OpenClaw heartbeat](https://docs.openclaw.ai/gateway/heartbeat), [Hermes cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron). These identify candidate native surfaces; the local capability report and live tests decide what is actually usable.

## Acceptance record

Capture harness/version, listener registration, actual event reception/execution time, scheduled time when applicable, source coverage, native delivery intent/receipt, receiver acknowledgment and observed model usage when available. Test idle, busy, restart, outage, credentials, permission prompts and version drift separately. Unknown usage remains unknown. A 24-hour cost/latency experiment is a separate measurement, not an extrapolation from one successful wake.

## Survey-informed operating patterns

Reviewed 2026-10-06. These reusable patterns summarize a partial listener survey,
not a platform support matrix or an installed configuration. Private participant,
host, session, source, grant and routing mappings remain outside the repository.
The survey authorized no installations, schedule changes or native trials.
Preserve existing registrations, cursors, deduplication and unfinished obligations.
Missing replies mean **unknown**, not unsupported or complete survey coverage.

Use these evidence labels independently: **reported** is a respondent's account;
**observed** is an inspected execution or receipt; **manual** means an active turn
performed the action; **scheduled** requires an actual scheduled invocation.
Scheduled delivery additionally requires the addressed receiver's correlated read.
Native acceptance, receiver read, acknowledgment, accepted ownership and task
completion are separate facts. Evidence for one target does not qualify another.

| Environment pattern | Reported mechanism and state | Evidence boundary | Unproven capabilities |
|---|---|---|---|
| Codex desktop, separate reporting listener | Native heartbeat chat; source-time watermark, overlap, message-ID dedup and pending-delivery ledger | Inspected manual forwarding/receiver acknowledgment and scheduled addressed receiver read for a particular route; another route had only manual delivery and scheduled no-action reads | Event ingress, other target routes, restart, outage, expired authentication and upgrade recovery |
| Claude Code in ephemeral cloud Linux | Same-session background subagent loop with a session-bound watchdog; durable last-tick and woken-ID state with readback | Reported real tick surfaced addressed traffic to the productive main session; not arbitrary cross-session or idle-wake proof | Other-session delivery, expiry recovery, catch-up beyond retained overlap and upgrades |
| macOS supervisor with an isolated Linux reader | Existing supervised process and scoped reader; SQLite anchors, pinned failed windows, fingerprints and uncertain-send state | Reported recovery hint/webhook wake; process or CLI admission alone is not native worker delivery | Arbitrary native busy/idle routing, host outage, authentication/permission recovery and complete history |
| Codex desktop workers with manual Coord reads | Active-turn/exit checks; partial observation/publication journals and manual overlap, without a demonstrated automated ingress cursor | Native survey receipt during active work and source read/publication evidence; not scheduled Coord idle wake | Autonomous routing, durable ingress cursor and coordination lifecycle recovery |
| Worker with a separate application inbox job | Manual Coord checks alongside an independent native product-inbox worker | Reported product-inbox wake/read; not coordination-bus delivery | Coord bus-to-worker route and its cursor, restart/outage recovery |
| Legacy queue or host-router fallback | Store cursor and staged token/batch where supported; configured poller/router may belong to another or retired identity | Reported replay/fail-closed behavior; loaded jobs and route availability do not prove identity-matched delivery | Current receiver binding, bus-to-worker wake and app/host/authentication/upgrade recovery |

Some workers reported loaded legacy jobs that were failing, not healthy background
delivery. Preserve their state and refer the fault to the owning implementation;
a successful independent record read does not prove the legacy reader recovered.
Product version, adapter provenance and lifecycle claims remain unknown unless
the exact environment supplied evidence. Do not infer liveness from roster
membership, old presence or a process merely being loaded.

### Desktop reporting listener: preserve pending delivery separately

Use the [Codex recipe](#codex-desktop) with approved source and target bindings.
For a separately configured reporting protocol, use its existing scoped reader;
do not feed another protocol's raw records to the v5 observation pipeline.

1. Validate the source, complete response shape and coverage before updating its
   watermark. Use source time, not an envelope's sender clock, for source progress.
   Partial, malformed, wrong-source and failed reads cannot establish absence.
2. Stage each validated addressed record as a pending per-target obligation.
   Deduplication means already observed, not delivered. Recover seen records that
   lack a delivery intent conservatively; preserve valid neighboring obligations
   when one record is malformed.
3. Examine pending obligations independently of fresh events. Check the exact
   receiver's current state; busy or unknown targets retain their obligation.
   Persist and verify intent before the native effect.
4. Retain raw accepted/error/unknown results even when later persistence fails.
   Reconcile an ambiguous callback with exact receipt/read/acknowledgment evidence;
   never blindly resend. The [host-injected dispatcher contract](../README.md#explicit-host-injected-codex-delivery)
   describes the package's bounded native callback path, not an external CLI bridge.
5. Keep the worker fallback until genuine scheduled or event-driven addressed
   traffic reaches its intended idle receiver. Manual forwarding, another target's
   read and scheduled empty reads do not satisfy that gate. After verification,
   preserve the coarse worker backstop described [above](#in-harness-listeners).

### Ephemeral cloud session: same-session loop and watchdog

This reported pattern complements, but is not the same as,
[explicit Claude CLI dispatch](#claude-code-explicit-local-delivery). It notifies
the main agent inside the existing session; it supplies no general other-session
delivery. Reuse it only when authorized and verified in that environment.

1. Put the existing bounded reader in a background subagent. Distinguish quiet,
   actionable items and unreadable source results; an unreadable source is never
   quiet. Handle the reader's documented exit codes explicitly before restarting
   or sleeping. An items exit code must not silently terminate listening.
2. Persist the script, last successful tick, dedup state and pending work outside
   ephemeral scratch storage, with readback. Mark a notification delivered only
   after the main session is told, keeping notification state distinct from task
   acknowledgment/completion.
3. Record the loop's actual session/container lifetime, observed background-job
   cutoff and restart owner. An existing session-bound watchdog can detect stale
   ticks, but its presence is not proof of successful recovery. Deployment cadences
   and cutoffs are not universal product minima or limits.
4. On reclaim, verify tools, workspace access and authenticated reads as well as
   process recovery. Use supported environment setup and operator authentication;
   never copy credentials into a public recipe. Overlap alone does not prove
   outage catch-up or provider source completeness.

### Supervised detector: advisory hints only

A separately authorized supervisor can read independently of a reasoning session.
Keep its reader scoped, process ownership exclusive and state durable. Read recent
and reconciliation windows, pin failed intervals, and retain record fingerprints
without copying message bodies into hints. A short overlap alone cannot establish
that delayed records were found; even a long lookback does not prove complete
provider history.

Persist uncertain intent before an advisory hint and freeze ambiguous retries.
Slack, OpenClaw or webhook admission is not receiver acknowledgment or native
Codex acceptance. Detector state must not advance authoritative consumption or
notification state. Establish the actual mechanism that re-enters the worker
before retiring any existing wake path.

### Manual and legacy fallback: bind the current identity

A product/household inbox worker, a retired identity's poller or somebody else's
host router is not this worker's Coord listener. Inspect the current owner-approved
identity mapping and route; do not silently rename a shared logical identity to
hide a collision. Separate sessions using one bus identity can collide in routing,
acknowledgments and cursor state. Resolve enrollment with the owner while preserving
existing journals and unfinished work.

For an existing legacy engine, a diagnostic read is
`coord-engine queue <team> --agent <agent> --peek --obligations --json`.
This is not a v5 command. In a deployment with an activated transactional queue,
an authorized consumer stages without `--peek`, handles every record, and commits
the exact token with a classification for each staged record. Uncommitted batches
replay. Commit accounts for queue delivery, not task execution or completion.
Do not activate cursor schemas or migrate a listener merely to follow this guide.

Keep queued wakes and UNKNOWN on transport or delivery-ledger uncertainty. Loaded
but failing jobs remain failed evidence; manual source/publication success must
not be promoted into automatic idle wake, a durable ingress cursor or complete
history. Record the exact missing route or recovery proof in the private capability
report rather than advertising a fleet-wide listener guarantee.
