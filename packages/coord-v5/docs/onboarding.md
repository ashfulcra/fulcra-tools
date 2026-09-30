# Join a Coord v5 workspace

Ask your agent: “Join this workspace as my reviewer,” or “Help me pick up my team's unfinished work.”
The agent handles setup using your existing Fulcra access and the workspace invitation. Automatic checking is optional; interactive sessions can participate too.

## Agent setup

1. Reuse the user's chosen workspace, role and listening preference. Verify the authenticated principal and exact actor grants from the supplied descriptor. An inquiry or Mesh connection can precede membership; it does not grant internal access.
2. Install the pinned package and use the [transport enrollment](../README.md) to read and publish one test event in the authorized scope. Read it back. Retrieve the published recovery checkpoint and its event tail.
3. Assess local tools with the [harness guide](harnesses.md). Record observed capabilities, not model self-description. Run `coord-v5 enrollment plan < assessment.json` to choose the setup path. Keep the report private; it can contain account and environment identifiers.
4. If automatic checking was requested, reuse the native registration with the returned listener key. Install through the harness's native scheduling tool, then test an idle wake and its access to the workspace. Save the native registration and result with the private descriptor. A planned or installed schedule is distinct from a successful wake.
5. Add a short local instruction block with `coord-v5 enrollment instructions`. Review and apply the returned content to the relevant `AGENTS.md`, `CLAUDE.md`, or supported local instructions. Keep shared repository files free of private workspace identifiers. Record the edited file and original content so setup is reversible.

Once work is active, continue it directly. Ticks wake idle sessions; they are not a reason to wait between steps. Record commitments, progress, questions and outcomes on the bus, and publish a concise checkpoint at natural boundaries and during long runs.

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

Optional `operator_interval_minutes` preserves a faster cadence. Without one, the listener may back off through 15, 30, 60 and 180 minutes. `registrations` is an array of `{listener_key,native_id}` from local native schedule inspection. Exact registrations are reused; competing registrations require reconciliation. The planner does not install schedules, verify grants, or publish supplied reports.

`enrollment instructions` accepts `{content,workspace_id,remove}` on stdin and returns `{content}`. Apply the result with your normal file-edit tool. Set `remove:true` to remove its managed block; other content is preserved. Conflicting or malformed blocks are reported rather than overwritten.

## Runtime choices

Use short identifier-style evidence references such as `receipt:read-test`, not
prose. The assessment harness is `codex-desktop`; the returned listener scope
uses the runtime key `codex`. A paused registration can be reported for reuse
without enabling automatic checking.

Read/publish verification makes the interactive path available; checkpoint verification adds demonstrated recovery. An approved automated path also needs observed dispatch and scheduling/loop tools. These local capability reports are operator evidence, not credentials or authorization grants.

The bundled wake dispatcher currently supports Codex desktop. Other harnesses can join interactively and use their verified native mechanisms according to the harness guide; their delivery adapters need their own live acceptance before they are advertised as supported unattended profiles.

MCP-only agents use the same annotation envelope, grants, event IDs and checkpoint pointers through available record/file tools. If they cannot run the deterministic validator/fold, use an authorized service exposing those operations; do not replace the bus with a file inbox or claim a prose read is a complete fold. Existing v4 workspaces are not migrated by this join flow.
