# Join a Coord v5 workspace

Ask your agent: “Join this workspace as my reviewer,” or “Help me pick up my team's unfinished work.”
The agent handles setup using your existing Fulcra access and the workspace invitation. Automatic checking is optional; interactive sessions can participate too.

## Agent setup

1. Reuse the user's chosen workspace, role and listening preference. Verify the authenticated principal and exact actor grants from the supplied descriptor. An inquiry or Mesh connection can precede membership; it does not grant internal access.
2. Install the pinned package and use the [transport enrollment](../README.md) to read and publish one test event in the authorized scope. Read it back. Retrieve the published recovery checkpoint and its event tail.
3. Assess local tools with the [harness guide](harnesses.md). Record observed capabilities, not model self-description. Run `coord-v5 enrollment plan < assessment.json` to choose the setup path. Keep the report private; it can contain account and environment identifiers.
4. If automatic checking was requested, reuse the exact native registration with the returned listener key. For a verified event path, configure the native event registration; scheduling is not a prerequisite. Use a verified schedule as a backstop or, without event ingress, a verified schedule/session loop as the fallback. Test actual event reception, native cross-session delivery and an idle worker's access to the workspace separately. Save registration and results with the private descriptor. A plan or registration is not successful delivery, idle wake or membership evidence.
5. Generate the appropriate local instruction block with `coord-v5 enrollment instructions`: omit `instruction_mode` for the worker block, or choose `listener` for reporting/routing and `executor` for explicitly owned-task execution. Review and apply the returned content to the relevant local/session `AGENTS.md`, `CLAUDE.md`, or supported instructions. Apply listener/executor blocks only to their own session, not shared repository-wide worker instructions. Use the [listener job recipe](harnesses.md#codex-desktop) for exact native preparation and receipts. Keep shared repository files free of private workspace identifiers. Record the edited file and original content so setup is reversible.

Once a worker owns active work, continue it directly; do not wait for ticks between steps. Prefer bounded subagents for scoped reasoning or implementation. A dedicated listener only receives, reports and routes attention to addressed workers. Deliberate self-ticking execution of an owned task is a separate opt-in mode, not the reporting-listener default. Workers record commitments, progress, questions and outcomes on the bus, and publish concise checkpoints at natural boundaries and during long runs.

## Shared Codex listener acceptance

For requested automatic checking, joining a Codex worker includes reconciling the
one listener for its environment/harness and registering the worker's exact
source, job and existing native session. Interactive membership and automatic
participation are separate outcomes: a successful read/publish or enrollment
receipt does not discharge unfinished listener setup.

- Inspect existing native sessions, registrations and private listener state
  first. Reuse the environment listener; a listener on a different machine is
  not evidence of local coverage. Add the exact route through the supported
  owner process, preserving existing sources, cursors, grants and pending effects.
- If no listener exists, record that fact and configure one only within the
  operator's existing setup authorization. Missing authorization, a source/job
  mapping, native route or supported wake mechanism becomes a linked owned task
  with the precise prerequisite and unblock action. Continue independent safe
  setup; do not silently replace the shared listener with a worker heartbeat.
- Prefer verified event ingress. If it is unavailable, use one shared scheduled
  listener when the operator permits polling. State that polling is the fallback;
  a local timer cannot invoke an app-only native messaging tool by itself.
- Record registration readback, actual event or scheduled invocation, successful
  source reads, native receipt, addressed idle worker delivery and independent
  receiver acknowledgment separately. A manual send or ACTIVE card is not
  scheduled proof. BUSY/UNKNOWN targets and ambiguous sends remain pending.
- Preserve any temporary worker fallback and monitoring obligations until their
  replacement is verified. Retire or relax it only within the approved scope;
  centralizing bus reads must not silently drop infrastructure incident coverage.

Keep this acceptance task visible at worker exit until verified or genuinely
blocked. Report interactive enrollment as successful with automatic delivery
pending when that is the evidence, rather than declaring the whole setup done.
These steps grant no permissions and install no listener or schedule themselves.

## Capability assessment

The pure planner takes this shape (invented identifiers):

```json
{
  "version": 1,
  "principal_id": "example-principal",
  "workspace_id": "example-workspace",
  "environment_id": "example-environment",
  "harness": "codex-desktop",
  "harness_version": "observed-version",
  "unattended": false,
  "evaluated_at": "2026-09-28T12:00:00Z",
  "capabilities": {
    "read": {"status": "verified", "observed_at": "2026-09-28T12:00:00Z", "evidence_ref": "receipt:read-test"},
    "publish": {"status": "unknown"},
    "checkpoint": {"status": "unknown"}
  }
}
```

Capabilities are `read`, `publish`, `checkpoint`, `dispatch`, `schedule`, `loop`, `event_ingress`, `idle_wake`, `busy_delivery`, `restart`, `host_outage`, `credential_loss`, `permission_prompt`, `upgrade`, and `notification`. Each is `verified`, `unsupported`, or `unknown`. Verified entries require a timestamp and an evidence reference; future or invalid timestamps are rejected. Evidence older than seven days becomes unknown; `evidence_max_age_hours` may shorten that window. Re-probe after changing harness version rather than copying evidence across versions.

Optional `operator_interval_minutes` preserves a faster fallback/backstop cadence. Without one, the listener may suggest backing off through 15, 30, 60 and 180 minutes. These interval fields do not require an event-only listener to poll. `registrations` is an array of `{listener_key,native_id}` from local native registration inspection. Exact registrations are reused across mode changes; competing registrations require reconciliation. The planner does not configure event ingress, install schedules, verify grants, or publish supplied reports.

`enrollment instructions` accepts `{content,workspace_id,remove}` plus optional `instruction_mode:"worker"|"listener"|"executor"` on stdin and returns `{content}` without writing files. For example (invented workspace):

```json
{"content":"Existing local rules","workspace_id":"example-workspace","remove":false,"instruction_mode":"listener"}
```

Apply the result with your normal file-edit tool. Switching modes replaces the one managed block; set `remove:true` with any valid mode to remove it and restore the surrounding bytes. Conflicting or malformed blocks and invalid mode values/types are refused; the CLI returns sanitized `INVALID_ENROLLMENT_INPUT`, including on removal. Omitted and explicit worker modes produce the same block; regenerating updates an older managed block while preserving surrounding text. Role selection is only instruction generation, independent of the planner's enrollment `mode`; it grants no permissions, ownership or schedule authority and does not prove agent obedience. Listener reads retain obligations under unknown/partial coverage, and the listener never fabricates receiver acknowledgment or reports worker progress/completion. Executor work requires explicit ownership and recovery context; missing context means report the gap, not adopt work. Self-ticking requires separate authorization and is never auto-created.

## Runtime choices

Use short identifier-style evidence references such as `receipt:read-test`, not
prose. The assessment harness is `codex-desktop`; the returned listener scope
uses the runtime key `codex`. A paused registration can be reported for reuse
without enabling automatic checking.

Read/publish verification makes the interactive path available; checkpoint verification adds demonstrated recovery. Every automated path requires current read, publish, checkpoint and dispatch evidence plus explicit unattended opt-in. Current `event_ingress` selects `native-event` without a schedule or loop and takes priority over a loop; a verified schedule adds `native-event-with-backstop`. Without current ingress, verified scheduling or loop tools select `native-scheduled` or `session-loop`. Stale or unknown ingress cannot select an event mode. These local capability reports are operator evidence, not credentials or authorization grants; event-mode selection alone does not establish idle wake or any delivery adapter's support.

The bundled wake dispatchers cover Codex desktop (`dispatchCodexWake`, called by the host) and the Claude Code CLI (`coord-v5 listener dispatch-claude`, resuming one bound session); see the [release status](https://github.com/ashfulcra/fulcra-tools/blob/main/docs/coord/V5-RELEASE-STATUS.md) for what each has demonstrated. Other harnesses can join interactively and use their verified native mechanisms according to the harness guide; their delivery adapters need their own live acceptance before they are advertised as supported unattended profiles.

MCP-only agents use the same annotation envelope, grants, event IDs and checkpoint pointers through available record/file tools. If they cannot run the deterministic validator/fold, use an authorized service exposing those operations; do not replace the bus with a file inbox or claim a prose read is a complete fold. Existing v4 workspaces are not migrated by this join flow.
