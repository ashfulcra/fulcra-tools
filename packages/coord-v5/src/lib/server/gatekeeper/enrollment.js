import { createHash } from "node:crypto";
import { exact, id, observation } from "./listener-validation.js";

const capabilityNames = [
  "read",
  "publish",
  "checkpoint",
  "dispatch",
  "schedule",
  "loop",
  "event_ingress",
  "idle_wake",
  "busy_delivery",
  "restart",
  "host_outage",
  "credential_loss",
  "permission_prompt",
  "upgrade",
  "notification",
];
const harnesses = [
  "codex-desktop",
  "codex-cloud",
  "claude-desktop",
  "claude-code",
  "claude-cloud",
  "chatgpt-web",
  "claude-web",
  "openclaw",
  "hermes",
  "other",
];

function timestamp(value) {
  // Use the same strict calendar/time validation as source observations.
  observation(
    {
      version: 1,
      observedAt: value,
      eventObservation: { coverage: "complete", items: [] },
      obligationObservation: { coverage: "complete", items: [] },
    },
    Number.MAX_SAFE_INTEGER,
    0,
  );
  return Date.parse(value);
}

/** A capability report is evidence supplied by a trusted local operator, never a bus grant.
 * Planning is pure: no credentials, file writes, schedules, membership or lifecycle changes. */
export function planEnrollment(input) {
  const value = exact(
    input,
    [
      "version",
      "principal_id",
      "workspace_id",
      "environment_id",
      "harness",
      "harness_version",
      "unattended",
      "evaluated_at",
      "capabilities",
    ],
    ["registrations", "operator_interval_minutes", "evidence_max_age_hours"],
  );
  if (
    value.version !== 1 ||
    !harnesses.includes(value.harness) ||
    typeof value.unattended !== "boolean"
  )
    throw new TypeError("Invalid enrollment");
  for (const key of [
    "principal_id",
    "workspace_id",
    "environment_id",
    "harness_version",
  ])
    id(value[key]);
  const now = timestamp(value.evaluated_at);
  const age = value.evidence_max_age_hours ?? 168;
  const interval = value.operator_interval_minutes ?? 180;
  if (
    !Number.isFinite(age) ||
    age <= 0 ||
    age > 168 ||
    !Number.isFinite(interval) ||
    interval <= 0 ||
    interval > 180
  )
    throw new TypeError("Invalid enrollment limits");
  const raw = exact(value.capabilities, [], capabilityNames);
  const capabilities = {};
  for (const name of capabilityNames) {
    if (!Object.hasOwn(raw, name)) {
      capabilities[name] = { status: "unknown" };
      continue;
    }
    const entry = exact(raw[name], ["status"], ["observed_at", "evidence_ref"]);
    if (!["verified", "unsupported", "unknown"].includes(entry.status))
      throw new TypeError("Invalid capability");
    if (
      entry.status === "verified" &&
      (!entry.observed_at || !entry.evidence_ref)
    )
      throw new TypeError("Verified capabilities require evidence");
    if (entry.evidence_ref !== undefined) id(entry.evidence_ref);
    let stale = false;
    if (entry.observed_at !== undefined) {
      const observed = timestamp(entry.observed_at);
      if (observed > now) throw new TypeError("Future capability evidence");
      stale = now - observed > age * 3_600_000;
    }
    capabilities[name] = {
      ...entry,
      ...(stale ? { status: "unknown", reason: "evidence_expired" } : {}),
    };
  }
  const verified = (name) => capabilities[name].status === "verified";
  const connected = verified("read") && verified("publish");
  let mode = connected ? "interactive" : "inquiry-only";
  if (
    connected &&
    verified("checkpoint") &&
    value.unattended &&
    verified("dispatch")
  ) {
    if (verified("event_ingress"))
      mode = verified("schedule") ? "native-event-with-backstop" : "native-event";
    else if (verified("schedule")) mode = "native-scheduled";
    else if (verified("loop")) mode = "session-loop";
  }
  const listenerKey = createHash("sha256")
    .update(
      JSON.stringify([
        value.principal_id,
        value.workspace_id,
        value.environment_id,
        value.harness,
      ]),
    )
    .digest("hex");
  if (
    value.registrations !== undefined &&
    (!Array.isArray(value.registrations) || value.registrations.length > 1000)
  )
    throw new TypeError("Invalid registrations");
  const matches = new Set();
  for (const rawRegistration of value.registrations ?? []) {
    const registration = exact(rawRegistration, ["listener_key", "native_id"]);
    id(registration.listener_key);
    id(registration.native_id);
    if (registration.listener_key === listenerKey)
      matches.add(registration.native_id);
  }
  if (matches.size > 1)
    throw new TypeError("Resolve competing listener registrations");
  const existing = [...matches][0];
  const automated = !["interactive", "inquiry-only"].includes(mode);
  const eventMode = ["native-event", "native-event-with-backstop"].includes(mode);
  return {
    version: 1,
    listener_key: listenerKey,
    mode,
    listener_scope: {
      principalId: value.principal_id,
      workspaceId: value.workspace_id,
      environmentId: value.environment_id,
      harness: value.harness === "codex-desktop" ? "codex" : value.harness,
    },
    membership: "requires_authorized_grant",
    deployment_status: existing ? "registration_reported" : "not_installed",
    deployment_action: existing
      ? { action: "reuse", native_id: existing }
      : {
          action: automated
            ? "install_with_native_harness_tool"
            : "check_on_turn",
        },
    service_level: !verified("checkpoint")
      ? "recovery-unverified"
      : automated
        ? verified("idle_wake")
          ? "idle-wake-verified"
          : "trial"
        : "interactive",
    initial_interval_minutes: Math.min(15, interval),
    operator_interval_minutes: interval,
    evaluated_at: value.evaluated_at,
    capabilities,
    lifecycle_gaps: [
      "busy_delivery",
      "restart",
      "host_outage",
      "credential_loss",
      "permission_prompt",
      "upgrade",
    ].filter((name) => !verified(name)),
    next_steps: [
      "Verify principal, workspace membership and exact actor grants with the owned-source transport.",
      "Round-trip one scoped event and retrieve a published checkpoint before advertising recovery.",
      ...(automated
        ? [
            eventMode
              ? "Reuse or configure the verified native event registration for this listener key; keep one listener per environment/harness. Observe real event reception and idle worker delivery before advertising that service level."
              : "Reuse the native registration for this listener key; keep one listener per environment/harness.",
          ]
        : []),
      "Read events and durable obligations together; dispatch only local configured role/job routes.",
      "Record native delivery receipts separately from worker acknowledgment and task progress.",
      mode === "native-event"
        ? "Keep the native event registration active, report read failures, and re-probe capabilities after harness updates."
        : mode === "native-event-with-backstop"
          ? "Keep the native event registration active and the schedule alive as a backstop, report read failures, and re-probe capabilities after harness updates."
          : mode === "session-loop"
            ? "Keep the session loop active, report read failures, and re-probe capabilities after harness updates."
            : automated
              ? "Keep the schedule alive, report read failures, and re-probe capabilities after harness updates."
              : "Continue interactively; automatic checking remains optional. Report read failures and re-probe capabilities after harness updates.",
    ],
  };
}

/** Return an idempotent reversible block; caller controls the local file edit. */
export function managedInstructions(
  content,
  workspaceId,
  remove = false,
  instructionMode = "worker",
) {
  id(workspaceId);
  if (
    typeof content !== "string" ||
    content.length > 1_000_000 ||
    typeof remove !== "boolean" ||
    !["worker", "listener", "executor"].includes(instructionMode)
  )
    throw new TypeError("Invalid instruction input");
  const start = `<!-- coord-v5:start ${workspaceId} -->`;
  const end = "<!-- coord-v5:end -->";
  const starts = [...content.matchAll(/<!-- coord-v5:start[^\n]*/g)];
  const ends = [...content.matchAll(/<!-- coord-v5:end -->/g)];
  if (starts.length || ends.length) {
    if (
      starts.length !== 1 ||
      ends.length !== 1 ||
      starts[0][0] !== start ||
      ends[0].index < starts[0].index
    )
      throw new TypeError("Different workspace or malformed managed block");
    const from = starts[0].index;
    let to = ends[0].index + end.length;
    if (content[to] === "\n") to++;
    const existing = content.slice(from, to);
    const replacement = remove ? "" : block(workspaceId, instructionMode);
    return content.replace(existing, replacement);
  }
  if (remove) return content;
  // No separator is inserted: removing this exact block restores caller content byte for byte.
  return content + block(workspaceId, instructionMode);
}

function block(workspaceId, instructionMode) {
  if (instructionMode === "listener")
    return `<!-- coord-v5:start ${workspaceId} -->
Coord v5 workspace: ${workspaceId}. Use the configured workspace descriptor and exact actor grants.
Reporting-only listener: read authorized addressed/mapped bus events and retained obligations together.
Unknown, failed or partial reads retain obligations; an empty event tail never proves all-clear.
Execute only exact prepared supported native message actions through locally configured authorized routes.
Report only your own reads, routing, conflicts and delivery reconciliation. Keep native receipts
separate from receiver acknowledgment and worker progress. Never fabricate or send a receiver
acknowledgment on a worker's behalf.
Do not claim or execute worker tasks, or publish their progress or completion on their behalf.
Prefer verified native event ingress; tick/loop fallback requires separate authorization.
These instructions install no event registration, schedule or loop and grant no permissions.
Use the installed Coord v5 onboarding and harness guides for exact preparation and receipt handling.
<!-- coord-v5:end -->
`;
  if (instructionMode === "executor")
    return `<!-- coord-v5:start ${workspaceId} -->
Coord v5 workspace: ${workspaceId}. Use the configured workspace descriptor and exact actor grants.
Owned-task executor: execute only explicitly authorized tasks you own, with verified recovery context.
A wake is not a grant. If ownership or recovery context is missing, report the gap; do not adopt work.
Resume owned obligations and continue reachable authorized work without waiting for ticks between steps.
Prefer bounded subagents for scoped reasoning or implementation while retaining responsibility.
Record commitments, progress, blockers and outcomes for your owned work through typed coordination events.
Before yielding, make the next action and recovery context durable. Report failed reads; never call them empty.
Unknown or partial reads retain obligations and cannot authorize takeover.
Direct self-ticking requires separate explicit executor authorization; never auto-create a schedule or loop.
These instructions install nothing and grant no permissions, ownership or unattended-operation authority.
Use the installed Coord v5 onboarding guide for enrollment and harness-specific execution.
<!-- coord-v5:end -->
`;
  return `<!-- coord-v5:start ${workspaceId} -->\nCoord v5 workspace: ${workspaceId}. Use the configured workspace descriptor and exact actor grants.\nAt work start, resume obligations. Record actionable commitments before substantial work.\nPublish progress, blockers and results through typed coordination events. Before yielding,\nmake the next action and recovery context durable. Report failed reads; never call them empty.\nUse the installed Coord v5 onboarding guide for enrollment and harness-specific listening.\n<!-- coord-v5:end -->\n`;
}
