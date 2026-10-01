import { describe, expect, it } from "vitest";
import { planEnrollment, managedInstructions } from "./enrollment.js";

const now = "2026-09-28T12:00:00Z";
const proof = () => ({
  status: "verified",
  observed_at: now,
  evidence_ref: "receipt:local-test",
});
const input = () => ({
  version: 1,
  principal_id: "account-example",
  workspace_id: "workspace-example",
  environment_id: "environment-example",
  harness: "codex-desktop",
  harness_version: "test-version",
  unattended: true,
  evaluated_at: now,
  capabilities: { read: proof(), publish: proof(), checkpoint: proof() },
});

describe("enrollment capability decisions", () => {
  it("allows interactive membership without making an idle wake promise", () => {
    const result = planEnrollment(input());
    expect(result.mode).toBe("interactive");
    expect(result.next_steps.join(" ")).not.toMatch(
      /Keep the schedule alive|Reuse the native registration/,
    );
    expect(result.deployment_status).toBe("not_installed");
    expect(result.membership).toBe("requires_authorized_grant");
  });
  it("selects an in-harness scheduler only from current observed evidence", () => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.schedule = proof();
    const result = planEnrollment(value);
    expect(result.mode).toBe("native-scheduled");
    expect(result.next_steps.join(" ")).toMatch(/Keep the schedule alive/);
    expect(result.service_level).toBe("trial");
    expect(result.listener_scope).toEqual({
      principalId: "account-example",
      workspaceId: "workspace-example",
      environmentId: "environment-example",
      harness: "codex",
    });
    value.capabilities.idle_wake = proof();
    expect(planEnrollment(value).service_level).toBe("idle-wake-verified");
    value.capabilities.schedule.observed_at = "2026-09-01T12:00:00Z";
    expect(planEnrollment(value).mode).toBe("interactive");
  });
  it("maintains a verified session loop without requiring an unknown schedule", () => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.loop = proof();
    const result = planEnrollment(value);
    expect(result.mode).toBe("session-loop");
    expect(result.capabilities.schedule.status).toBe("unknown");
    expect(result.next_steps.join(" ")).toMatch(/Keep the session loop active/);
    expect(result.next_steps.join(" ")).not.toMatch(/Keep the schedule alive/);
  });
  it("selects native events without a schedule or loop and does not promise idle delivery", () => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = proof();
    const result = planEnrollment(value);
    expect(result.mode).toBe("native-event");
    expect(result.deployment_action).toEqual({ action: "install_with_native_harness_tool" });
    expect(result.service_level).toBe("trial");
    expect(result.membership).toBe("requires_authorized_grant");
    expect(result.deployment_status).toBe("not_installed");
    expect(result.capabilities.idle_wake.status).toBe("unknown");
    expect(result.next_steps.join(" ")).toMatch(/native event registration/);
    expect(result.next_steps.join(" ")).toMatch(/reception.*idle worker delivery/i);
    expect(result.next_steps.join(" ")).not.toMatch(/Keep the schedule alive|cadence|interval/);
  });
  it("prefers native events over a verified session loop", () => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = proof();
    value.capabilities.loop = proof();
    expect(planEnrollment(value).mode).toBe("native-event");
  });
  it("retains the scheduled backstop when event ingress and schedule are verified", () => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = proof();
    value.capabilities.schedule = proof();
    value.capabilities.loop = proof();
    const result = planEnrollment(value);
    expect(result.mode).toBe("native-event-with-backstop");
    expect(result.next_steps.join(" ")).toMatch(/native event registration/);
    expect(result.next_steps.join(" ")).toMatch(/schedule.*backstop/i);
  });
  it.each([
    ["unknown", "schedule", "native-scheduled"],
    ["unsupported", "loop", "session-loop"],
    ["stale", "schedule", "native-scheduled"],
    ["stale", "loop", "session-loop"],
    ["stale", null, "interactive"],
  ])("uses fallback for %s ingress with %s", (status, fallback, mode) => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = status === "stale"
      ? { ...proof(), observed_at: "2026-09-21T11:59:59Z" }
      : { status };
    if (fallback) value.capabilities[fallback] = proof();
    const result = planEnrollment(value);
    expect(result.mode).toBe(mode);
    expect(result.capabilities.event_ingress.status).toBe(status === "stale" ? "unknown" : status);
  });
  it("accepts ingress at the evidence-age boundary but drops an expired backstop", () => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = { ...proof(), observed_at: "2026-09-21T12:00:00Z" };
    value.capabilities.schedule = { ...proof(), observed_at: "2026-09-21T11:59:59Z" };
    expect(planEnrollment(value).mode).toBe("native-event");
    value.evidence_max_age_hours = 24;
    expect(planEnrollment(value).mode).toBe("interactive");
  });
  it.each([
    ["read", "inquiry-only"],
    ["publish", "inquiry-only"],
    ["checkpoint", "interactive"],
    ["dispatch", "interactive"],
  ])("does not automate native events without current %s evidence", (capability, mode) => {
    const value = input();
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = proof();
    value.capabilities[capability] = { ...proof(), observed_at: "2026-09-01T12:00:00Z" };
    expect(planEnrollment(value).mode).toBe(mode);
    delete value.capabilities[capability];
    expect(planEnrollment(value).mode).toBe(mode);
  });
  it("does not let native event evidence bypass unattended opt-in", () => {
    const value = input();
    value.unattended = false;
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = proof();
    value.capabilities.schedule = proof();
    value.capabilities.loop = proof();
    expect(planEnrollment(value).mode).toBe("interactive");
  });
  it("reuses only the exact listener registration across event and fallback modes", () => {
    const value = input();
    const key = planEnrollment(value).listener_key;
    value.capabilities.dispatch = proof();
    value.capabilities.event_ingress = proof();
    value.registrations = [
      { listener_key: "different-key", native_id: "unrelated-registration" },
      { listener_key: key, native_id: "event-registration" },
      { listener_key: key, native_id: "event-registration" },
    ];
    const result = planEnrollment(value);
    expect(result.mode).toBe("native-event");
    expect(result.listener_key).toBe(key);
    expect(result.deployment_status).toBe("registration_reported");
    expect(result.deployment_action).toEqual({ action: "reuse", native_id: "event-registration" });
    expect(result.membership).toBe("requires_authorized_grant");
    expect(result.service_level).toBe("trial");
    value.capabilities.schedule = proof();
    expect(planEnrollment(value).listener_key).toBe(key);
    value.registrations.push({ listener_key: key, native_id: "competing-registration" });
    expect(() => planEnrollment(value)).toThrow(/competing/);
  });
  it("binds one listener key to environment/harness, not a worker session or chosen mode", () => {
    const value = input();
    const first = planEnrollment(value);
    value.capabilities.loop = proof();
    value.capabilities.dispatch = proof();
    const second = planEnrollment(value);
    expect(second.mode).toBe("session-loop");
    expect(second.listener_key).toBe(first.listener_key);
    value.environment_id = "different-environment";
    expect(planEnrollment(value).listener_key).not.toBe(first.listener_key);
  });
  it("requires explicit unattended opt-in even when all tools are available", () => {
    const value = input();
    value.unattended = false;
    value.capabilities.schedule = proof();
    value.capabilities.dispatch = proof();
    expect(planEnrollment(value).mode).toBe("interactive");
  });
  it("does not infer permissions from a known harness or partial connection", () => {
    const value = input();
    value.capabilities.read.status = "unknown";
    expect(planEnrollment(value).mode).toBe("inquiry-only");
    value.capabilities.read = proof();
    delete value.capabilities.checkpoint;
    expect(planEnrollment(value).service_level).toBe("recovery-unverified");
  });
  it.each(["secret", "shell_command"])(
    "rejects unexpected top-level input %s",
    (key) => {
      const value = input();
      value[key] = "not-accepted";
      expect(() => planEnrollment(value)).toThrow();
    },
  );
  it("rejects future or malformed proofs and unverifiable verified claims", () => {
    const value = input();
    value.capabilities.read.observed_at = "2026-09-29T12:00:00Z";
    expect(() => planEnrollment(value)).toThrow();
    value.capabilities.read.observed_at = "2026-02-30T12:00:00Z";
    expect(() => planEnrollment(value)).toThrow();
    value.capabilities.read = { status: "verified" };
    expect(() => planEnrollment(value)).toThrow();
  });
  it("reuses an exact registration but refuses competing listener instances", () => {
    const value = input();
    const key = planEnrollment(value).listener_key;
    value.registrations = [{ listener_key: key, native_id: "schedule-one" }];
    expect(planEnrollment(value).deployment_action).toEqual({
      action: "reuse",
      native_id: "schedule-one",
    });
    value.registrations.push({ listener_key: key, native_id: "schedule-two" });
    expect(() => planEnrollment(value)).toThrow(/competing/);
  });
  it("retains faster operator cadence and does not treat stale evidence as verified", () => {
    const value = input();
    value.operator_interval_minutes = 5;
    expect(planEnrollment(value).initial_interval_minutes).toBe(5);
    value.operator_interval_minutes = 0;
    expect(() => planEnrollment(value)).toThrow();
  });
  it("leaves idle backoff available when no faster operator interval was chosen", () => {
    expect(planEnrollment(input()).operator_interval_minutes).toBe(180);
  });
});

describe("managed local instruction block", () => {
  // Catches worker-byte drift independently of the generator's implementation.
  const worker = "<!-- coord-v5:start workspace-example -->\n" +
    "Coord v5 workspace: workspace-example. Use the configured workspace descriptor and exact actor grants.\n" +
    "At work start, resume obligations. Record actionable commitments before substantial work.\n" +
    "Publish progress, blockers and results through typed coordination events. Before yielding,\n" +
    "make the next action and recovery context durable. Report failed reads; never call them empty.\n" +
    "Use the installed Coord v5 onboarding guide for enrollment and harness-specific listening.\n" +
    "<!-- coord-v5:end -->\n";
  const modes = ["worker", "listener", "executor"];
  it("keeps omitted, undefined and explicit worker output byte-identical", () => {
    for (const mode of [undefined, "worker"])
      expect(managedInstructions("prefix", "workspace-example", false, mode)).toBe("prefix" + worker);
    expect(managedInstructions("prefix", "workspace-example")).toBe("prefix" + worker);
  });
  // Catches ignored selection, stale replacement, duplicate blocks and damaged edges.
  // These assertions measure text transformation, not agent instruction-following.
  it.each(modes)("selects distinct %s content and preserves all mode transitions", (source) => {
    const selected = managedInstructions("", "workspace-example", false, source);
    if (source !== "worker") expect(selected).not.toBe(worker);
    expect(managedInstructions("", "workspace-example", false, "listener")).not.toBe(
      managedInstructions("", "workspace-example", false, "executor"),
    );
    for (const original of ["", "prefix", "prefix\n", "prefix\r\n"]) {
      const added = managedInstructions(original, "workspace-example", false, source);
      for (const suffix of ["", "suffix"]) {
        const surrounding = added + suffix;
        for (const target of modes) {
          const changed = managedInstructions(surrounding, "workspace-example", false, target);
          if (source !== target) expect(changed).not.toBe(surrounding);
          expect(changed.startsWith(original)).toBe(true);
          expect(changed.endsWith("<!-- coord-v5:end -->\n" + suffix)).toBe(true);
          expect(changed.match(/<!-- coord-v5:start /g)).toHaveLength(1);
          expect(changed.match(/<!-- coord-v5:end -->/g)).toHaveLength(1);
          expect(managedInstructions(changed, "workspace-example", false, target)).toBe(changed);
          expect(managedInstructions(changed, "workspace-example")).toBe(original + worker + suffix);
          for (const removeMode of modes)
            expect(managedInstructions(changed, "workspace-example", true, removeMode)).toBe(original + suffix);
        }
      }
    }
  });
  // Catches validation bypass on add, replacement and both removal early returns.
  it.each(["unknown", "", null, 0, false, {}, [], "Listener"].map(mode => ({ mode })))("rejects invalid mode $mode on every branch", ({ mode }) => {
    for (const content of ["prefix", "prefix" + worker + "suffix"])
      for (const remove of [false, true])
        expect(() => managedInstructions(content, "workspace-example", remove, mode)).toThrow(TypeError);
  });
  // Catches role selection weakening workspace/fence or existing input validation.
  it.each(modes)("retains exact input and fence refusals for %s", (mode) => {
    for (const content of [worker + worker, worker.replace("workspace-example", "workspace-other"),
      "<!-- coord-v5:start workspace-example -->\n", "<!-- coord-v5:end -->",
      "<!-- coord-v5:end -->\n<!-- coord-v5:start workspace-example -->\n"])
      for (const remove of [false, true])
        expect(() => managedInstructions(content, "workspace-example", remove, mode)).toThrow();
    expect(() => managedInstructions(null, "workspace-example", false, mode)).toThrow();
    expect(() => managedInstructions("x".repeat(1_000_001), "workspace-example", false, mode)).toThrow();
    expect(() => managedInstructions("", "workspace-example", "false", mode)).toThrow();
    expect(() => managedInstructions("", "", false, mode)).toThrow();
    expect(managedInstructions("prefix", "workspace-example", true, mode)).toBe("prefix");
  });
  it("preserves existing content and reruns without duplicate blocks", () => {
    const original = "# Local instructions\n\nKeep my rules.\n";
    const added = managedInstructions(original, "workspace-example");
    expect(added.startsWith(original)).toBe(true);
    expect(managedInstructions(added, "workspace-example")).toBe(added);
    expect(managedInstructions(added, "workspace-example", true)).toBe(
      original,
    );
  });
  it("does not overwrite a different workspace or malformed managed block", () => {
    const added = managedInstructions("User content\n", "workspace-a");
    expect(() => managedInstructions(added, "workspace-b")).toThrow();
    expect(() =>
      managedInstructions(
        "<!-- coord-v5:start workspace-a -->\n",
        "workspace-a",
      ),
    ).toThrow();
  });
});
